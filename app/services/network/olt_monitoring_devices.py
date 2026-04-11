"""OLT monitoring-device resolution helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice
from app.models.network_monitoring import NetworkDevice


def find_linked_network_device(
    db: Session,
    *,
    mgmt_ip: str | None,
    hostname: str | None,
    name: str,
) -> NetworkDevice | None:
    """Find the monitoring device that represents an OLT."""
    if mgmt_ip:
        matched = db.scalars(
            select(NetworkDevice).where(NetworkDevice.mgmt_ip == mgmt_ip)
        ).first()
        if matched:
            return matched
    if hostname:
        matched = db.scalars(
            select(NetworkDevice).where(NetworkDevice.hostname == hostname)
        ).first()
        if matched:
            return matched
    return db.scalars(select(NetworkDevice).where(NetworkDevice.name == name)).first()


def resolve_snmp_target_for_olt(db: Session, olt: OLTDevice) -> Any | None:
    """Resolve a SNMP-capable target for an OLT, falling back to OLT SNMP fields."""
    linked = find_linked_network_device(
        db,
        mgmt_ip=olt.mgmt_ip,
        hostname=olt.hostname,
        name=olt.name,
    )
    if linked is not None:
        return linked

    raw_ro = getattr(olt, "snmp_ro_community", None)
    if raw_ro and raw_ro.strip():
        return SimpleNamespace(
            id=None,
            mgmt_ip=olt.mgmt_ip,
            hostname=olt.hostname,
            snmp_enabled=True,
            snmp_community=raw_ro.strip(),
            snmp_version="v2c",
            snmp_port=None,
            vendor=olt.vendor,
        )
    return None
