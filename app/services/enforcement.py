"""Enforcement helpers for sessions, throttling, and blocking."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from pyrad.client import Client, Timeout
from pyrad.dictionary import Dictionary
from pyrad.packet import DisconnectRequest
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
from app.services.nas import DeviceProvisioner
from app.services.radius import sync_credential_to_radius

logger = logging.getLogger(__name__)


def _setting_bool(db: Session, domain: SettingDomain, key: str, default: bool) -> bool:
    value = settings_spec.resolve_value(db, domain, key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _radius_dictionary_path(db: Session) -> str | None:
    return (
        settings_spec.resolve_value(db, SettingDomain.radius, "coa_dictionary_path")
        or settings_spec.resolve_value(db, SettingDomain.radius, "auth_dictionary_path")
    )


def _radius_timeout_sec(db: Session) -> float:
    timeout = settings_spec.resolve_value(db, SettingDomain.radius, "coa_timeout_sec")
    try:
        return float(timeout) if timeout is not None else 3.0
    except (TypeError, ValueError):
        return 3.0


def _coa_enabled(db: Session) -> bool:
    return _setting_bool(db, SettingDomain.radius, "coa_enabled", True)


def _coa_retries(db: Session) -> int:
    retries = settings_spec.resolve_value(db, SettingDomain.radius, "coa_retries")
    try:
        return int(retries) if retries is not None else 1
    except (TypeError, ValueError):
        return 1


def _mikrotik_kill_enabled(db: Session) -> bool:
    return _setting_bool(db, SettingDomain.network, "mikrotik_session_kill_enabled", True)


def _address_list_block_enabled(db: Session) -> bool:
    return _setting_bool(db, SettingDomain.network, "address_list_block_enabled", True)


def _default_address_list(db: Session) -> str | None:
    return settings_spec.resolve_value(
        db, SettingDomain.network, "default_mikrotik_address_list"
    )


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
    client = Client(
        server=host,
        secret=nas_device.shared_secret.encode("utf-8"),
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
        DeviceProvisioner._execute_ssh(
            nas_device,
            f'/ppp active remove [find where name="{username}"]',
        )
        return True
    except Exception as exc:
        logger.warning("MikroTik session disconnect failed: %s", exc)
        return False


def _apply_mikrotik_address_list(
    nas_device: NasDevice, list_name: str, address: str
) -> bool:
    if nas_device.vendor != NasVendor.mikrotik:
        return False


def _remove_mikrotik_address_list(
    nas_device: NasDevice, list_name: str, address: str
) -> bool:
    if nas_device.vendor != NasVendor.mikrotik:
        return False
    try:
        DeviceProvisioner._execute_ssh(
            nas_device,
            f'/ip firewall address-list remove [find list="{list_name}" address="{address}"]',
        )
        return True
    except Exception as exc:
        logger.warning("MikroTik address-list removal failed: %s", exc)
        return False
    try:
        DeviceProvisioner._execute_ssh(
            nas_device,
            f'/ip firewall address-list add list="{list_name}" address="{address}"',
        )
        return True
    except Exception as exc:
        logger.warning("MikroTik address-list update failed: %s", exc)
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
        .filter(Subscription.account_id == coerce_uuid(account_id))
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
        .filter(AccessCredential.account_id == coerce_uuid(account_id))
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
