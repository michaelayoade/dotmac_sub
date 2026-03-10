"""Reject-IP enforcement and router rule push helpers."""

from __future__ import annotations

import ipaddress
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.radius import RadiusClient, RadiusServer
from app.models.subscription_engine import SettingValueType
from app.services.common import coerce_uuid
from app.services.enforcement import _sanitize_routeros_value
from app.services.nas import DeviceProvisioner

logger = logging.getLogger(__name__)


REJECT_IP_KEYS: dict[str, str] = {
    "not_found": "reject_ip_not_found",
    "blocked": "reject_ip_blocked",
    "negative": "reject_ip_negative",
    "bad_mac": "reject_ip_bad_mac",
    "bad_password": "reject_ip_bad_password",
}
REJECT_REASON_ALIASES: dict[str, str] = {
    "not_found": "not_found",
    "not-found": "not_found",
    "blocked": "blocked",
    "block": "blocked",
    "negative": "negative",
    "negative_balance": "negative",
    "negative-balance": "negative",
    "dunning": "negative",
    "bad_mac": "bad_mac",
    "bad-mac": "bad_mac",
    "bad_password": "bad_password",
    "bad-password": "bad_password",
}
_RUNTIME_STATE_KEY = "reject_ip_runtime_state"
_RUNTIME_STATE_VERSION = 1
_INITIAL_PUSH_KEY = "reject_ip_initial_push_done_at"
_STATUS_BLOCKED = {
    SubscriptionStatus.suspended,
    SubscriptionStatus.canceled,
    SubscriptionStatus.expired,
}


def _get_setting(db: Session, key: str) -> DomainSetting | None:
    return db.scalars(
        select(DomainSetting)
        .where(DomainSetting.domain == SettingDomain.radius)
        .where(DomainSetting.key == key)
    ).first()


def _get_setting_text(db: Session, key: str) -> str:
    setting = _get_setting(db, key)
    if not setting:
        return ""
    if setting.value_text is not None:
        return str(setting.value_text).strip()
    if isinstance(setting.value_json, str):
        return setting.value_json.strip()
    return ""


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _captive_portal_config(db: Session) -> dict[str, str | bool]:
    enabled = _truthy(_get_setting_text(db, "captive_redirect_enabled"))
    portal_ip = _get_setting_text(db, "captive_portal_ip")
    portal_url = _get_setting_text(db, "captive_portal_url")
    if not portal_ip and portal_url:
        # Best effort fallback for users who save only URL.
        parsed = urlparse(portal_url)
        portal_ip = (parsed.hostname or "").strip()
    return {
        "enabled": enabled,
        "portal_ip": portal_ip,
        "portal_url": portal_url,
    }


def _load_runtime_state(db: Session) -> dict[str, Any]:
    setting = _get_setting(db, _RUNTIME_STATE_KEY)
    if setting and isinstance(setting.value_json, dict):
        raw = dict(setting.value_json)
        subscriptions = raw.get("subscriptions")
        if isinstance(subscriptions, dict):
            return {
                "version": int(raw.get("version") or _RUNTIME_STATE_VERSION),
                "subscriptions": subscriptions,
            }
    return {"version": _RUNTIME_STATE_VERSION, "subscriptions": {}}


def _save_runtime_state(db: Session, state: dict[str, Any]) -> None:
    setting = _get_setting(db, _RUNTIME_STATE_KEY)
    if setting:
        setting.value_type = SettingValueType.json
        setting.value_json = state
        setting.value_text = None
        setting.is_active = True
        return
    db.add(
        DomainSetting(
            domain=SettingDomain.radius,
            key=_RUNTIME_STATE_KEY,
            value_type=SettingValueType.json,
            value_json=state,
            value_text=None,
            is_active=True,
            is_secret=False,
        )
    )


def get_reject_networks(db: Session) -> dict[str, ipaddress.IPv4Network]:
    networks: dict[str, ipaddress.IPv4Network] = {}
    for reason, key in REJECT_IP_KEYS.items():
        raw = _get_setting_text(db, key)
        if not raw:
            continue
        try:
            net = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            logger.warning("Invalid reject IP CIDR for %s: %s", key, raw)
            continue
        if net.version != 4:
            logger.warning("Reject IP CIDR must be IPv4 for %s: %s", key, raw)
            continue
        networks[reason] = net
    return networks


def normalize_reject_reason(reason: str | None) -> str:
    raw = str(reason or "").strip().lower()
    return REJECT_REASON_ALIASES.get(raw, "blocked")


def _is_in_any_reject_network(
    ip_text: str | None, networks: dict[str, ipaddress.IPv4Network]
) -> bool:
    if not ip_text:
        return False
    try:
        ip_obj = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    if ip_obj.version != 4:
        return False
    return any(ip_obj in net for net in networks.values())


def _used_ipv4_in_network(
    db: Session,
    *,
    network: ipaddress.IPv4Network,
    exclude_subscription_id: str,
) -> set[str]:
    used: set[str] = set()
    candidates = db.execute(
        select(Subscription.id, Subscription.ipv4_address, Subscription.status)
        .where(Subscription.id != coerce_uuid(exclude_subscription_id))
        .where(Subscription.ipv4_address.is_not(None))
    ).all()
    for _, ip_text, status in candidates:
        if status not in _STATUS_BLOCKED:
            continue
        if not ip_text:
            continue
        try:
            ip_obj = ipaddress.ip_address(str(ip_text))
        except ValueError:
            continue
        if ip_obj.version == 4 and ip_obj in network:
            used.add(str(ip_obj))
    return used


def _pick_reject_ipv4(
    db: Session,
    *,
    subscription: Subscription,
    network: ipaddress.IPv4Network,
) -> str:
    host_count = int(network.num_addresses) - 2
    if host_count <= 0:
        raise ValueError(f"Reject network has no usable host addresses: {network}")
    used = _used_ipv4_in_network(
        db,
        network=network,
        exclude_subscription_id=str(subscription.id),
    )
    first = int(network.network_address) + 1
    start_offset = hash(str(subscription.id)) % host_count
    for idx in range(host_count):
        candidate_int = first + ((start_offset + idx) % host_count)
        candidate = str(ipaddress.IPv4Address(candidate_int))
        if candidate not in used:
            return candidate
    raise ValueError(f"No free reject IP left in {network}")


def enforce_subscription_reject_ip(
    db: Session,
    subscription_id: str,
    reject_reason: str | None = None,
) -> dict[str, Any]:
    """Apply or restore reject IP assignment based on subscription status."""
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription:
        return {"ok": False, "changed": False, "reason": "subscription_not_found"}

    networks = get_reject_networks(db)
    state = _load_runtime_state(db)
    subscriptions = state.setdefault("subscriptions", {})
    sub_id = str(subscription.id)
    entry = subscriptions.get(sub_id, {})
    now_iso = datetime.now(UTC).isoformat()

    if subscription.status == SubscriptionStatus.active:
        original_ipv4 = str(entry.get("original_ipv4") or "").strip()
        changed = False
        if original_ipv4 and original_ipv4 != str(subscription.ipv4_address or "").strip():
            subscription.ipv4_address = original_ipv4
            changed = True
        if sub_id in subscriptions:
            subscriptions.pop(sub_id, None)
            _save_runtime_state(db, state)
        return {
            "ok": True,
            "changed": changed,
            "mode": "restore",
            "ip": subscription.ipv4_address,
        }

    if subscription.status not in _STATUS_BLOCKED:
        return {"ok": True, "changed": False, "mode": "noop"}

    reject_key = normalize_reject_reason(reject_reason)
    target_network = networks.get(reject_key)
    if not target_network and reject_key != "blocked":
        reject_key = "blocked"
        target_network = networks.get(reject_key)
    if not target_network:
        setting_key = REJECT_IP_KEYS.get(reject_key, REJECT_IP_KEYS["blocked"])
        return {
            "ok": False,
            "changed": False,
            "reason": f"{setting_key}_not_configured",
        }

    current_ip = str(subscription.ipv4_address or "").strip()
    if not entry.get("original_ipv4") and current_ip and not _is_in_any_reject_network(current_ip, networks):
        entry["original_ipv4"] = current_ip

    assigned_ip = str(entry.get("reject_ipv4") or "").strip()
    try:
        assigned_obj = ipaddress.ip_address(assigned_ip) if assigned_ip else None
    except ValueError:
        assigned_obj = None
    if not assigned_obj or assigned_obj.version != 4 or assigned_obj not in target_network:
        assigned_ip = _pick_reject_ipv4(
            db,
            subscription=subscription,
            network=target_network,
        )

    changed = current_ip != assigned_ip
    if changed:
        subscription.ipv4_address = assigned_ip

    entry.update(
        {
            "reject_key": REJECT_IP_KEYS[reject_key],
            "reject_reason": reject_key,
            "reject_ipv4": assigned_ip,
            "status": subscription.status.value,
            "updated_at": now_iso,
        }
    )
    subscriptions[sub_id] = entry
    _save_runtime_state(db, state)
    return {"ok": True, "changed": changed, "mode": "block", "ip": assigned_ip}


def _radius_connected_nas_devices(db: Session) -> list[NasDevice]:
    devices = db.scalars(
        select(NasDevice)
        .join(RadiusClient, RadiusClient.nas_device_id == NasDevice.id)
        .join(RadiusServer, RadiusServer.id == RadiusClient.server_id)
        .where(NasDevice.is_active.is_(True))
        .where(RadiusClient.is_active.is_(True))
        .where(RadiusServer.is_active.is_(True))
    ).all()
    by_id: dict[str, NasDevice] = {str(device.id): device for device in devices}
    return list(by_id.values())


def _nat_target_ip(portal_ip: str) -> str:
    # RouterOS dst-nat target expects an IP; trim CIDR suffix if present.
    text = portal_ip.strip()
    if "/" in text:
        text = text.split("/", 1)[0].strip()
    return text


def _firewall_commands(
    networks: dict[str, ipaddress.IPv4Network],
    *,
    captive_enabled: bool = False,
    captive_portal_ip: str = "",
) -> list[str]:
    commands: list[str] = []
    for reason in ("not_found", "blocked", "negative", "bad_mac", "bad_password"):
        network = networks.get(reason)
        if not network:
            continue
        list_name = _sanitize_routeros_value(f"dotmac-reject-{reason.replace('_', '-')}")
        net_text = _sanitize_routeros_value(str(network))
        rule_comment = _sanitize_routeros_value(f"dotmac-reject-drop-{reason}")
        commands.extend(
            [
                f'/ip firewall address-list remove [find list="{list_name}"]',
                f'/ip firewall address-list add list="{list_name}" address="{net_text}" comment="dotmac reject {reason}"',
                f'/ip firewall filter remove [find comment="{rule_comment}"]',
            ]
        )
        if reason == "negative" and captive_enabled and captive_portal_ip:
            portal_text = _sanitize_routeros_value(captive_portal_ip)
            portal_target = _sanitize_routeros_value(_nat_target_ip(captive_portal_ip))
            allow_comment = "dotmac-negative-allow-portal"
            redirect_comment = "dotmac-negative-redirect-http"
            https_drop_comment = "dotmac-negative-drop-https"
            commands.extend(
                [
                    f'/ip firewall filter remove [find comment="{allow_comment}"]',
                    f'/ip firewall nat remove [find comment="{redirect_comment}"]',
                    f'/ip firewall filter remove [find comment="{https_drop_comment}"]',
                    f'/ip firewall filter add chain=forward src-address-list="{list_name}" dst-address="{portal_text}" protocol=tcp dst-port=443 action=accept comment="{allow_comment}"',
                    f'/ip firewall nat add chain=dstnat src-address-list="{list_name}" protocol=tcp dst-port=80 action=dst-nat to-addresses={portal_target} to-ports=80 comment="{redirect_comment}"',
                    f'/ip firewall filter add chain=forward src-address-list="{list_name}" dst-address=!{portal_text} protocol=tcp dst-port=443 action=drop comment="{https_drop_comment}"',
                ]
            )
        commands.append(
            f'/ip firewall filter add chain=forward src-address-list="{list_name}" action=drop comment="{rule_comment}"'
        )
    return commands


def push_reject_rules_to_radius_nas(db: Session) -> dict[str, Any]:
    """Push reject CIDR address-lists and drop rules to connected MikroTik NAS devices."""
    networks = get_reject_networks(db)
    if not networks:
        return {"ok": False, "message": "No reject IP ranges configured.", "pushed": 0, "failed": 0}

    devices = _radius_connected_nas_devices(db)
    if not devices:
        return {"ok": False, "message": "No RADIUS-connected NAS devices found.", "pushed": 0, "failed": 0}

    captive = _captive_portal_config(db)
    commands = _firewall_commands(
        networks,
        captive_enabled=bool(captive.get("enabled")),
        captive_portal_ip=str(captive.get("portal_ip") or ""),
    )
    if not commands:
        return {"ok": False, "message": "No valid reject CIDR rules to push.", "pushed": 0, "failed": 0}

    pushed = 0
    failed = 0
    failures: list[str] = []
    for device in devices:
        if str(getattr(device.vendor, "value", device.vendor)) != "mikrotik":
            continue
        try:
            for cmd in commands:
                try:
                    DeviceProvisioner._execute_ssh(device, cmd)
                except Exception:
                    # Safe to ignore "remove [find ...]" misses during idempotent refresh.
                    if " remove [find " in cmd:
                        continue
                    raise
            pushed += 1
        except Exception as exc:
            failed += 1
            failures.append(f"{device.name}: {exc}")

    if pushed == 0 and failed == 0:
        return {"ok": False, "message": "No MikroTik NAS devices matched the RADIUS-connected set.", "pushed": 0, "failed": 0}

    if failed:
        return {
            "ok": pushed > 0,
            "message": f"Pushed rules to {pushed} router(s); {failed} failed.",
            "pushed": pushed,
            "failed": failed,
            "failures": failures,
        }

    return {
        "ok": True,
        "message": f"Pushed reject rules to {pushed} router(s).",
        "pushed": pushed,
        "failed": 0,
    }


def push_reject_rules_once(db: Session) -> dict[str, Any]:
    """Push reject rules once (bootstrap), then mark completion in settings."""
    existing = _get_setting(db, _INITIAL_PUSH_KEY)
    already_done = (
        existing is not None
        and (
            str(existing.value_text or "").strip()
            or (
                isinstance(existing.value_json, str)
                and existing.value_json.strip()
            )
        )
    )
    if already_done:
        return {"ok": True, "message": "Initial reject rule push already completed."}

    result = push_reject_rules_to_radius_nas(db)
    if not result.get("ok"):
        return result

    pushed_at = datetime.now(UTC).isoformat()
    if existing:
        existing.value_type = SettingValueType.string
        existing.value_text = pushed_at
        existing.value_json = None
        existing.is_active = True
    else:
        db.add(
            DomainSetting(
                domain=SettingDomain.radius,
                key=_INITIAL_PUSH_KEY,
                value_type=SettingValueType.string,
                value_text=pushed_at,
                value_json=None,
                is_active=True,
                is_secret=False,
            )
        )
    db.commit()
    result["bootstrap"] = True
    return result
