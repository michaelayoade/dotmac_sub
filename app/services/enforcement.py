"""Enforcement helpers for sessions, throttling, and blocking."""

from __future__ import annotations

import logging
import re

from fastapi import HTTPException
from pyrad.client import Client, Timeout
from pyrad.dictionary import Dictionary
from pyrad.packet import CoARequest, DisconnectRequest
from sqlalchemy.orm import Session

from app.models.catalog import (
    AccessCredential,
    NasDevice,
    NasVendor,
    OfferRadiusProfile,
    RadiusProfile,
    Subscription,
)
from app.models.domain_settings import SettingDomain
from app.models.radius import RadiusClient
from app.models.usage import AccountingStatus, RadiusAccountingSession
from app.services import settings_spec
from app.services.common import coerce_uuid
from app.services.credential_crypto import decrypt_credential
from app.services.nas import DeviceProvisioner
from app.services.radius import sync_credential_to_radius

logger = logging.getLogger(__name__)

# Characters that could break RouterOS CLI quoting or inject commands
_ROUTEROS_UNSAFE_RE = re.compile(r'[";\\{}\n\r]')


def _sanitize_routeros_value(value: str) -> str:
    """Remove characters that could break RouterOS CLI quoting."""
    return _ROUTEROS_UNSAFE_RE.sub("", value)


def _setting_bool(db: Session, domain: SettingDomain, key: str, default: bool) -> bool:
    value = settings_spec.resolve_value(db, domain, key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _radius_dictionary_path(db: Session) -> str | None:
    value = settings_spec.resolve_value(db, SettingDomain.radius, "coa_dictionary_path")
    if value is None:
        value = settings_spec.resolve_value(
            db, SettingDomain.radius, "auth_dictionary_path"
        )
    return None if value is None else str(value)


def _radius_timeout_sec(db: Session) -> float:
    timeout = settings_spec.resolve_value(db, SettingDomain.radius, "coa_timeout_sec")
    try:
        return float(str(timeout)) if timeout is not None else 3.0
    except (TypeError, ValueError):
        return 3.0


def _coa_enabled(db: Session) -> bool:
    return _setting_bool(db, SettingDomain.radius, "coa_enabled", True)


def _coa_retries(db: Session) -> int:
    retries = settings_spec.resolve_value(db, SettingDomain.radius, "coa_retries")
    try:
        return int(str(retries)) if retries is not None else 1
    except (TypeError, ValueError):
        return 1


def _mikrotik_kill_enabled(db: Session) -> bool:
    return _setting_bool(db, SettingDomain.network, "mikrotik_session_kill_enabled", True)


def _address_list_block_enabled(db: Session) -> bool:
    return _setting_bool(db, SettingDomain.network, "address_list_block_enabled", True)


def _default_address_list(db: Session) -> str | None:
    value = settings_spec.resolve_value(
        db, SettingDomain.network, "default_mikrotik_address_list"
    )
    return None if value is None else str(value)


def _resolve_effective_profile(
    db: Session, subscription: Subscription
) -> RadiusProfile | None:
    if subscription.radius_profile_id:
        profile = db.get(RadiusProfile, subscription.radius_profile_id)
        if profile:
            return profile
    offer_profile = (
        db.query(OfferRadiusProfile)
        .filter(OfferRadiusProfile.offer_id == subscription.offer_id)
        .first()
    )
    if offer_profile:
        return db.get(RadiusProfile, offer_profile.profile_id)
    return None


def _resolve_nas_device(
    db: Session, session: RadiusAccountingSession
) -> NasDevice | None:
    if session.nas_device_id:
        return db.get(NasDevice, session.nas_device_id)
    if session.radius_client_id:
        client = db.get(RadiusClient, session.radius_client_id)
        if client and client.nas_device_id:
            return db.get(NasDevice, client.nas_device_id)
    return None


def _send_coa_disconnect(
    db: Session,
    nas_device: NasDevice,
    username: str | None,
    framed_ip: str | None,
    session_id: str | None,
) -> bool:
    if not _coa_enabled(db):
        return False
    if not nas_device.shared_secret:
        logger.warning("Missing NAS shared secret for CoA disconnect.")
        return False
    host = nas_device.nas_ip or nas_device.management_ip or nas_device.ip_address
    if not host:
        logger.warning("Missing NAS host for CoA disconnect.")
        return False
    dict_path = _radius_dictionary_path(db)
    if not dict_path:
        logger.warning("Missing RADIUS dictionary path for CoA disconnect.")
        return False
    try:
        dictionary = Dictionary(dict_path)
    except Exception as exc:
        logger.warning("Failed to load RADIUS dictionary: %s", exc)
        return False
    # Decrypt the shared secret before use
    decrypted_secret = decrypt_credential(nas_device.shared_secret)
    if decrypted_secret is None:
        logger.warning("Missing NAS shared secret for CoA disconnect.")
        return False
    client = Client(
        server=host,
        secret=decrypted_secret.encode("utf-8"),
        dict=dictionary,
        coaport=int(nas_device.coa_port or 3799),
    )
    client.retries = _coa_retries(db)
    client.timeout = _radius_timeout_sec(db)
    req = client.CreateCoAPacket(code=DisconnectRequest)
    if username:
        req["User-Name"] = username
    if framed_ip:
        req["Framed-IP-Address"] = framed_ip
    if session_id:
        req["Acct-Session-Id"] = session_id
    try:
        client.SendPacket(req)
        return True
    except Timeout:
        logger.warning("CoA disconnect timed out for NAS %s.", nas_device.id)
        return False
    except Exception as exc:
        logger.warning("CoA disconnect failed for NAS %s: %s", nas_device.id, exc)
        return False


def _build_mikrotik_rate_limit(profile: RadiusProfile) -> str | None:
    """Build MikroTik-Rate-Limit attribute string from profile speeds.

    Format: rx/tx (download is rx from NAS perspective, upload is tx).
    Supports burst: rx/tx burst-rx/burst-tx threshold-rx/threshold-tx time
    """
    if profile.mikrotik_rate_limit:
        return profile.mikrotik_rate_limit
    if not profile.download_speed and not profile.upload_speed:
        return None
    dl = f"{profile.download_speed}k" if profile.download_speed else "0"
    ul = f"{profile.upload_speed}k" if profile.upload_speed else "0"
    rate = f"{dl}/{ul}"
    if profile.burst_download or profile.burst_upload:
        bdl = f"{profile.burst_download}k" if profile.burst_download else dl
        bul = f"{profile.burst_upload}k" if profile.burst_upload else ul
        threshold = f"{profile.burst_threshold}k" if profile.burst_threshold else dl
        threshold_ul = f"{profile.burst_threshold}k" if profile.burst_threshold else ul
        btime = str(profile.burst_time or 10)
        rate = f"{dl}/{ul} {bdl}/{bul} {threshold}/{threshold_ul} {btime}/{btime}"
    return rate


def _send_coa_update(
    db: Session,
    nas_device: NasDevice,
    username: str | None,
    framed_ip: str | None,
    session_id: str | None,
    profile: RadiusProfile,
) -> bool:
    """Send a RADIUS CoA-Update to change session attributes in-place.

    This sends a CoA-Request (code 43) with updated bandwidth/profile
    attributes, allowing mid-session speed changes without disconnecting
    the subscriber.
    """
    if not _coa_enabled(db):
        return False
    if not nas_device.shared_secret:
        logger.warning("Missing NAS shared secret for CoA update.")
        return False
    host = nas_device.nas_ip or nas_device.management_ip or nas_device.ip_address
    if not host:
        logger.warning("Missing NAS host for CoA update.")
        return False
    dict_path = _radius_dictionary_path(db)
    if not dict_path:
        logger.warning("Missing RADIUS dictionary path for CoA update.")
        return False
    try:
        dictionary = Dictionary(dict_path)
    except Exception as exc:
        logger.warning("Failed to load RADIUS dictionary: %s", exc)
        return False
    decrypted_secret = decrypt_credential(nas_device.shared_secret)
    if decrypted_secret is None:
        logger.warning("Missing NAS shared secret for CoA update.")
        return False
    client = Client(
        server=host,
        secret=decrypted_secret.encode("utf-8"),
        dict=dictionary,
        coaport=int(nas_device.coa_port or 3799),
    )
    client.retries = _coa_retries(db)
    client.timeout = _radius_timeout_sec(db)
    req = client.CreateCoAPacket(code=CoARequest)
    if username:
        req["User-Name"] = username
    if framed_ip:
        req["Framed-IP-Address"] = framed_ip
    if session_id:
        req["Acct-Session-Id"] = session_id
    # Apply profile bandwidth attributes
    rate_limit = _build_mikrotik_rate_limit(profile)
    if rate_limit:
        try:
            req["Mikrotik-Rate-Limit"] = rate_limit
        except KeyError:
            logger.debug("Mikrotik-Rate-Limit attribute not in dictionary, skipping.")
    if profile.download_speed:
        try:
            req["Filter-Id"] = profile.name or profile.code or ""
        except KeyError:
            pass
    try:
        client.SendPacket(req)
        logger.info(
            "CoA update sent for user=%s on NAS %s (profile=%s).",
            username, nas_device.id, profile.name,
        )
        return True
    except Timeout:
        logger.warning("CoA update timed out for NAS %s.", nas_device.id)
        return False
    except Exception as exc:
        logger.warning("CoA update failed for NAS %s: %s", nas_device.id, exc)
        return False


def update_subscription_sessions(
    db: Session, subscription_id: str, reason: str | None = None
) -> int:
    """Send CoA-Update to active sessions to apply new profile in-place.

    Falls back to disconnect+reconnect if CoA-Update is not supported
    or fails.
    """
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")
    profile = _resolve_effective_profile(db, subscription)
    if not profile:
        logger.warning("No profile found for subscription %s, skipping CoA update.", subscription_id)
        return 0
    sessions = (
        db.query(RadiusAccountingSession)
        .filter(RadiusAccountingSession.subscription_id == subscription.id)
        .filter(RadiusAccountingSession.session_end.is_(None))
        .filter(RadiusAccountingSession.status_type != AccountingStatus.stop)
        .all()
    )
    if not sessions:
        return 0
    count = 0
    for session in sessions:
        credential = db.get(AccessCredential, session.access_credential_id)
        nas_device = _resolve_nas_device(db, session)
        username = credential.username if credential else None
        framed_ip = subscription.ipv4_address
        session_id = session.session_id
        if nas_device:
            if _send_coa_update(db, nas_device, username, framed_ip, session_id, profile):
                count += 1
            else:
                # Fall back to disconnect — subscriber reconnects with new profile
                if _send_coa_disconnect(db, nas_device, username, framed_ip, session_id):
                    count += 1
                elif _disconnect_mikrotik_session(db, nas_device, username):
                    count += 1
    if count:
        logger.info(
            "Updated %s active sessions for subscription %s (%s).",
            count, subscription_id, reason or "profile_change",
        )
    return count


def _disconnect_mikrotik_session(
    db: Session, nas_device: NasDevice, username: str | None
) -> bool:
    if not username:
        return False
    if nas_device.vendor != NasVendor.mikrotik:
        return False
    if not _mikrotik_kill_enabled(db):
        return False
    try:
        # Try PPPoE first
        safe_user = _sanitize_routeros_value(username)
        DeviceProvisioner._execute_ssh(
            nas_device,
            f'/ppp active remove [find where name="{safe_user}"]',
        )
        return True
    except Exception as exc:
        logger.warning("MikroTik PPP session disconnect failed: %s", exc)
        return False


def _disconnect_mikrotik_hotspot_session(
    db: Session, nas_device: NasDevice, username: str | None
) -> bool:
    """Disconnect an active MikroTik hotspot session."""
    if not username:
        return False
    if nas_device.vendor != NasVendor.mikrotik:
        return False
    if not _mikrotik_kill_enabled(db):
        return False
    try:
        safe_user = _sanitize_routeros_value(username)
        DeviceProvisioner._execute_ssh(
            nas_device,
            f'/ip hotspot active remove [find user="{safe_user}"]',
        )
        return True
    except Exception as exc:
        logger.warning("MikroTik hotspot session disconnect failed: %s", exc)
        return False


def _apply_mikrotik_address_list(
    nas_device: NasDevice, list_name: str, address: str
) -> bool:
    if nas_device.vendor != NasVendor.mikrotik:
        return False
    try:
        safe_list = _sanitize_routeros_value(list_name)
        safe_addr = _sanitize_routeros_value(address)
        DeviceProvisioner._execute_ssh(
            nas_device,
            f'/ip firewall address-list add list="{safe_list}" address="{safe_addr}"',
        )
        return True
    except Exception as exc:
        logger.warning("MikroTik address-list update failed: %s", exc)
        return False


def _remove_mikrotik_address_list(
    nas_device: NasDevice, list_name: str, address: str
) -> bool:
    if nas_device.vendor != NasVendor.mikrotik:
        return False
    try:
        safe_list = _sanitize_routeros_value(list_name)
        safe_addr = _sanitize_routeros_value(address)
        DeviceProvisioner._execute_ssh(
            nas_device,
            f'/ip firewall address-list remove [find list="{safe_list}" address="{safe_addr}"]',
        )
        return True
    except Exception as exc:
        logger.warning("MikroTik address-list removal failed: %s", exc)
        return False


def disconnect_subscription_sessions(
    db: Session, subscription_id: str, reason: str | None = None
) -> int:
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")
    sessions = (
        db.query(RadiusAccountingSession)
        .filter(RadiusAccountingSession.subscription_id == subscription.id)
        .filter(RadiusAccountingSession.session_end.is_(None))
        .filter(RadiusAccountingSession.status_type != AccountingStatus.stop)
        .all()
    )
    if not sessions:
        return 0
    count = 0
    for session in sessions:
        credential = db.get(AccessCredential, session.access_credential_id)
        nas_device = _resolve_nas_device(db, session)
        username = credential.username if credential else None
        framed_ip = subscription.ipv4_address
        session_id = session.session_id
        if nas_device:
            if _send_coa_disconnect(db, nas_device, username, framed_ip, session_id):
                count += 1
            elif _disconnect_mikrotik_session(db, nas_device, username):
                count += 1
            elif _disconnect_mikrotik_hotspot_session(db, nas_device, username):
                count += 1
    if count:
        logger.info(
            "Disconnected %s active sessions for subscription %s (%s).",
            count,
            subscription_id,
            reason or "no_reason",
        )
    return count


def disconnect_account_sessions(
    db: Session, account_id: str, reason: str | None = None
) -> int:
    subscriptions = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == coerce_uuid(account_id))
        .all()
    )
    count = 0
    for sub in subscriptions:
        count += disconnect_subscription_sessions(db, str(sub.id), reason=reason)
    return count


def apply_radius_profile_to_account(
    db: Session, account_id: str, profile_id: str
) -> int:
    credentials = (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == coerce_uuid(account_id))
        .filter(AccessCredential.is_active.is_(True))
        .all()
    )
    if not credentials:
        return 0
    profile = db.get(RadiusProfile, coerce_uuid(profile_id))
    if not profile or not profile.is_active:
        raise HTTPException(status_code=404, detail="RADIUS profile not found")
    updated = 0
    for cred in credentials:
        if cred.radius_profile_id != profile.id:
            cred.radius_profile_id = profile.id
            updated += 1
    db.commit()
    for cred in credentials:
        try:
            sync_credential_to_radius(db, cred)
        except Exception as exc:
            logger.warning(
                "Failed to sync credential %s to RADIUS: %s",
                cred.username,
                exc,
            )
    return updated


def apply_subscription_address_list_block(
    db: Session, subscription_id: str
) -> int:
    if not _address_list_block_enabled(db):
        return 0
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription:
        return 0
    profile = _resolve_effective_profile(db, subscription)
    list_name = profile.mikrotik_address_list if profile else None
    list_name = list_name or _default_address_list(db)
    if not list_name:
        return 0
    if not subscription.ipv4_address:
        return 0
    sessions = (
        db.query(RadiusAccountingSession)
        .filter(RadiusAccountingSession.subscription_id == subscription.id)
        .filter(RadiusAccountingSession.session_end.is_(None))
        .filter(RadiusAccountingSession.status_type != AccountingStatus.stop)
        .all()
    )
    count = 0
    targets: list[NasDevice] = []
    for session in sessions:
        nas_device = _resolve_nas_device(db, session)
        if nas_device:
            targets.append(nas_device)
    if not targets and subscription.provisioning_nas_device_id:
        nas_device = db.get(NasDevice, subscription.provisioning_nas_device_id)
        if nas_device:
            targets.append(nas_device)
    for nas_device in targets:
        if _apply_mikrotik_address_list(
            nas_device, list_name, subscription.ipv4_address
        ):
            count += 1
    return count


def remove_subscription_address_list_block(
    db: Session, subscription_id: str
) -> int:
    if not _address_list_block_enabled(db):
        return 0
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription:
        return 0
    profile = _resolve_effective_profile(db, subscription)
    list_name = profile.mikrotik_address_list if profile else None
    list_name = list_name or _default_address_list(db)
    if not list_name:
        return 0
    if not subscription.ipv4_address:
        return 0
    sessions = (
        db.query(RadiusAccountingSession)
        .filter(RadiusAccountingSession.subscription_id == subscription.id)
        .filter(RadiusAccountingSession.session_end.is_(None))
        .filter(RadiusAccountingSession.status_type != AccountingStatus.stop)
        .all()
    )
    count = 0
    targets: list[NasDevice] = []
    for session in sessions:
        nas_device = _resolve_nas_device(db, session)
        if nas_device:
            targets.append(nas_device)
    if not targets and subscription.provisioning_nas_device_id:
        nas_device = db.get(NasDevice, subscription.provisioning_nas_device_id)
        if nas_device:
            targets.append(nas_device)
    for nas_device in targets:
        if _remove_mikrotik_address_list(
            nas_device, list_name, subscription.ipv4_address
        ):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Subscription cancellation cleanup
# ---------------------------------------------------------------------------


def cleanup_subscription_on_cancel(
    db: Session, subscription_id: str
) -> dict[str, int]:
    """Full cleanup when a subscription is canceled.

    1. Disconnect all active RADIUS sessions
    2. Deactivate RADIUS credentials
    3. Remove credentials from external RADIUS DB
    4. Release IP assignments
    5. Remove NAS user entries
    6. Clean up address list entries

    Returns:
        Dict with counts of each cleanup action
    """
    from app.models.network import IPAssignment
    from app.models.radius import RadiusUser

    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription:
        return {"error": 1}

    stats: dict[str, int] = {
        "sessions_disconnected": 0,
        "credentials_deactivated": 0,
        "radius_users_removed": 0,
        "ip_assignments_released": 0,
        "nas_commands_sent": 0,
    }

    # 1. Disconnect active sessions
    try:
        stats["sessions_disconnected"] = disconnect_subscription_sessions(
            db, subscription_id, reason="canceled",
        )
    except Exception as exc:
        logger.warning("Session disconnect on cancel failed: %s", exc)

    # 2. Deactivate RADIUS credentials for this subscriber
    credentials = (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == subscription.subscriber_id)
        .filter(AccessCredential.is_active.is_(True))
        .all()
    )
    # Only deactivate if no other active subscriptions for this subscriber
    other_active = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == subscription.subscriber_id)
        .filter(Subscription.id != subscription.id)
        .filter(Subscription.status.in_(["active", "pending"]))
        .first()
    )
    if not other_active:
        for cred in credentials:
            cred.is_active = False
            stats["credentials_deactivated"] += 1
        # 3. Remove from external RADIUS DB
        _remove_credentials_from_external_radius(db, credentials)
        # Remove internal RadiusUser records
        for cred in credentials:
            radius_users = (
                db.query(RadiusUser)
                .filter(RadiusUser.access_credential_id == cred.id)
                .all()
            )
            for ru in radius_users:
                ru.is_active = False
                stats["radius_users_removed"] += 1

    # 4. Release IP assignments
    ip_assignments = (
        db.query(IPAssignment)
        .filter(IPAssignment.subscription_id == subscription.id)
        .filter(IPAssignment.is_active.is_(True))
        .all()
    )
    for assignment in ip_assignments:
        assignment.is_active = False
        stats["ip_assignments_released"] += 1
    # Clear IP from subscription
    subscription.ipv4_address = None
    subscription.ipv6_address = None

    # 5. Remove NAS user entries
    if subscription.provisioning_nas_device_id:
        nas_device = db.get(NasDevice, subscription.provisioning_nas_device_id)
        if nas_device:
            try:
                from app.services.connection_type_provisioning import (
                    build_nas_provisioning_commands,
                )
                profile = _resolve_effective_profile(db, subscription)
                commands = build_nas_provisioning_commands(
                    db, subscription, nas_device, profile=profile, action="delete",
                )
                for cmd in commands:
                    try:
                        DeviceProvisioner._execute_ssh(nas_device, cmd)
                        stats["nas_commands_sent"] += 1
                    except Exception as cmd_exc:
                        logger.warning("NAS cleanup command failed: %s", cmd_exc)
            except Exception as exc:
                logger.warning("NAS cleanup failed for subscription %s: %s", subscription_id, exc)

    # 6. Remove address list entries
    try:
        remove_subscription_address_list_block(db, subscription_id)
    except Exception as exc:
        logger.warning("Address list cleanup failed: %s", exc)

    db.flush()
    logger.info(
        "Subscription %s cancellation cleanup: %s",
        subscription_id, stats,
    )
    return stats


def _remove_credentials_from_external_radius(
    db: Session, credentials: list[AccessCredential]
) -> None:
    """Remove credentials from all external RADIUS databases."""
    from app.models.radius import RadiusSyncJob

    if not credentials:
        return
    sync_jobs = (
        db.query(RadiusSyncJob)
        .filter(RadiusSyncJob.is_active.is_(True))
        .filter(RadiusSyncJob.sync_users.is_(True))
        .filter(RadiusSyncJob.connector_config_id.isnot(None))
        .all()
    )
    if not sync_jobs:
        return
    for job in sync_jobs:
        try:
            from app.services.radius import _external_db_config
            config = _external_db_config(db, job)
            if not config:
                continue
            _delete_users_from_external_radius(config, credentials)
        except Exception as exc:
            logger.warning("External RADIUS cleanup failed for job %s: %s", job.id, exc)


def _delete_users_from_external_radius(
    config: dict,
    credentials: list[AccessCredential],
) -> None:
    """Delete user entries from an external FreeRADIUS database."""
    from sqlalchemy import create_engine, text

    radcheck = config["radcheck_table"]
    radreply = config["radreply_table"]
    radusergroup = config["radusergroup_table"]
    use_group = config["use_group"]

    engine = create_engine(config["db_url"])
    with engine.begin() as conn:
        for credential in credentials:
            username = credential.username
            conn.execute(text(f"DELETE FROM {radcheck} WHERE username = :u"), {"u": username})  # noqa: S608
            conn.execute(text(f"DELETE FROM {radreply} WHERE username = :u"), {"u": username})  # noqa: S608
            if use_group:
                conn.execute(
                    text(f"DELETE FROM {radusergroup} WHERE username = :u"), {"u": username}  # noqa: S608
                )
