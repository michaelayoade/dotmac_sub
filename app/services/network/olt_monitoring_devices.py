"""OLT monitoring-device resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice
from app.models.network_monitoring import NetworkDevice


@dataclass(frozen=True)
class OltMonitoringResolution:
    """Resolved monitoring-device link for an OLT detail view."""

    device: NetworkDevice | None
    match_strategy: str
    authoritative: bool
    warning: str | None = None


def resolve_linked_network_device(db: Session, olt: object) -> OltMonitoringResolution:
    """Resolve the monitoring device that represents an OLT with source metadata."""
    if olt.mgmt_ip:
        matched = db.scalars(
            select(NetworkDevice).where(NetworkDevice.mgmt_ip == olt.mgmt_ip)
        ).first()
        if matched:
            return OltMonitoringResolution(
                device=matched,
                match_strategy="mgmt_ip",
                authoritative=True,
            )
    if olt.hostname:
        matched = db.scalars(
            select(NetworkDevice).where(NetworkDevice.hostname == olt.hostname)
        ).first()
        if matched:
            return OltMonitoringResolution(
                device=matched,
                match_strategy="hostname",
                authoritative=False,
                warning="Monitoring device matched by hostname because no management IP match was found.",
            )
    matched = db.scalars(select(NetworkDevice).where(NetworkDevice.name == olt.name)).first()
    if matched:
        return OltMonitoringResolution(
            device=matched,
            match_strategy="name",
            authoritative=False,
            warning="Monitoring device matched by name because no management IP or hostname match was found.",
        )
    return OltMonitoringResolution(
        device=None,
        match_strategy="none",
        authoritative=False,
        warning="No linked monitoring device was found for this OLT.",
    )


def find_linked_network_device(
    db: Session,
    *,
    mgmt_ip: str | None,
    hostname: str | None,
    name: str,
) -> NetworkDevice | None:
    """Find the monitoring device that represents an OLT."""
    olt = SimpleNamespace(mgmt_ip=mgmt_ip, hostname=hostname, name=name)
    return resolve_linked_network_device(db, olt).device


def resolve_snmp_target_for_olt(db: Session, olt: OLTDevice) -> Any | None:
    """Resolve a SNMP-capable target for an OLT, falling back to OLT SNMP fields."""
    linked = resolve_linked_network_device(db, olt).device
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
