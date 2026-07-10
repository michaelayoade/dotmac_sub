"""Backfill mgmt IPs for UISP-named topology devices (inventory cleanup).

The Zabbix reconcile created hundreds of ``network_devices`` rows named
``uisp-<uuid>`` for trapper-only UISP hosts — no mgmt IP, so the native
poller can't probe them and they sit at ``live_status = unknown``, capping
outage-detection confidence. The UUID in the name IS the UISP device id, and
UISP knows the device's IP: this backfill resolves name → UISP device → IP
and stamps empty ``mgmt_ip`` columns (plus ``uisp_device_id`` where it is
safely claimable), after which the ordinary poll sweep takes over.

Idempotent and additive: rows that already have a mgmt_ip are never touched,
and an IP already claimed by another device is skipped (mgmt_ip is unique) —
those conflicts are reported, not resolved.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.network_monitoring import NetworkDevice

logger = logging.getLogger(__name__)

_UISP_NAME_RE = re.compile(
    r"^uisp-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)


def _uuid_from_name(device: NetworkDevice) -> str | None:
    for value in (device.name, device.hostname):
        match = _UISP_NAME_RE.match(str(value or "").strip())
        if match:
            return match.group(1).lower()
    return None


def backfill_uisp_mgmt_ips(
    session: Session, client, *, dry_run: bool = False
) -> dict[str, int]:
    """Stamp mgmt_ip (and uisp_device_id where free) from the UISP device list.

    Returns counters; with ``dry_run`` nothing is written.
    """
    from app.services.topology.uisp_sync import _device_id, _mgmt_ip

    candidates = list(
        session.scalars(
            select(NetworkDevice)
            .where(NetworkDevice.is_active.is_(True))
            .where(NetworkDevice.mgmt_ip.is_(None))
            .where(
                or_(
                    NetworkDevice.uisp_device_id.isnot(None),
                    NetworkDevice.name.ilike("uisp-%"),
                    NetworkDevice.hostname.ilike("uisp-%"),
                )
            )
        ).all()
    )
    result = {
        "candidates": len(candidates),
        "matched_in_uisp": 0,
        "stamped_ip": 0,
        "stamped_uisp_id": 0,
        "no_ip_in_uisp": 0,
        "not_in_uisp": 0,
        "ip_conflicts": 0,
    }
    if not candidates:
        return result

    uisp_by_id: dict[str, dict] = {}
    for payload in client.list_devices():
        uisp_id = _device_id(payload)
        if uisp_id:
            uisp_by_id[uisp_id.lower()] = payload

    claimed_ips = {
        ip
        for ip in session.scalars(
            select(NetworkDevice.mgmt_ip).where(NetworkDevice.mgmt_ip.isnot(None))
        )
    }
    claimed_uisp_ids = {
        str(uid).lower()
        for uid in session.scalars(
            select(NetworkDevice.uisp_device_id).where(
                NetworkDevice.uisp_device_id.isnot(None)
            )
        )
    }

    for device in candidates:
        uisp_id: str | None = (
            str(device.uisp_device_id).lower()
            if device.uisp_device_id
            else _uuid_from_name(device)
        )
        payload = uisp_by_id.get(uisp_id) if uisp_id else None
        if payload is None:
            result["not_in_uisp"] += 1
            continue
        result["matched_in_uisp"] += 1

        ip = _mgmt_ip(payload)
        if not ip:
            result["no_ip_in_uisp"] += 1
            continue
        if ip in claimed_ips:
            result["ip_conflicts"] += 1
            logger.info(
                "uisp_ip_backfill_conflict device=%s ip=%s already claimed",
                device.name,
                ip,
            )
            continue

        if not dry_run:
            device.mgmt_ip = ip
        claimed_ips.add(ip)
        result["stamped_ip"] += 1

        # Claim the uisp_device_id link too when nothing else holds it
        # (partial-unique index) — it lets uisp_sync keep this row fresh.
        if (
            uisp_id
            and device.uisp_device_id is None
            and uisp_id not in claimed_uisp_ids
        ):
            if not dry_run:
                device.uisp_device_id = uisp_id
            claimed_uisp_ids.add(uisp_id)
            result["stamped_uisp_id"] += 1

    if not dry_run:
        session.flush()
    return result
