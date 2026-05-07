"""Helpers for imported OLT service-port state."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OltServicePort, OntUnit
from app.services.network.olt_ssh import ServicePortEntry


class ImportedServicePortStateMissing(RuntimeError):
    """Raised when an OLT has no imported service-port source-of-truth rows."""


def service_port_entry_from_import(port: OltServicePort) -> ServicePortEntry:
    """Convert an imported DB row to the parser DTO used by OLT actions."""
    return ServicePortEntry(
        index=port.port_index,
        vlan_id=port.vlan_id,
        ont_id=port.ont_id_on_olt,
        gem_index=port.gem_index,
        flow_type=port.flow_type or "vlan",
        flow_para=port.flow_para or (str(port.user_vlan) if port.user_vlan else ""),
        state=port.state or "unknown",
        fsp=port.fsp,
        tag_transform=port.tag_transform or "",
    )


def list_imported_service_ports(
    db: Session,
    *,
    olt_id: UUID | str,
    fsp: str | None = None,
    ont_id_on_olt: int | None = None,
) -> list[ServicePortEntry]:
    """Return imported service-port rows for an OLT, optionally scoped to an ONT."""
    olt_uuid = UUID(str(olt_id)) if isinstance(olt_id, str) else olt_id
    stmt = select(OltServicePort).where(OltServicePort.olt_device_id == olt_uuid)
    if fsp is not None:
        stmt = stmt.where(OltServicePort.fsp == fsp)
    if ont_id_on_olt is not None:
        stmt = stmt.where(OltServicePort.ont_id_on_olt == ont_id_on_olt)
    stmt = stmt.order_by(OltServicePort.port_index)
    return [service_port_entry_from_import(port) for port in db.scalars(stmt).all()]


def has_imported_service_port_state(
    db: Session,
    *,
    olt_id: UUID | str,
) -> bool:
    """Return True when any service-port state has been imported for this OLT."""
    olt_uuid = UUID(str(olt_id)) if isinstance(olt_id, str) else olt_id
    stmt = (
        select(OltServicePort.id)
        .where(OltServicePort.olt_device_id == olt_uuid)
        .limit(1)
    )
    return db.scalars(stmt).first() is not None


def require_imported_service_port_state(
    db: Session,
    *,
    olt_id: UUID | str,
) -> None:
    """Fail unless imported service-port rows exist for this OLT."""
    if not has_imported_service_port_state(db, olt_id=olt_id):
        raise ImportedServicePortStateMissing(
            "No imported service-port state exists for this OLT. "
            "Run Import OLT State from a running configuration before provisioning."
        )


def imported_service_port_summary(
    db: Session,
    *,
    olt_id: UUID | str,
) -> dict[str, object]:
    """Return aggregate visibility for imported service-port state."""
    olt_uuid = UUID(str(olt_id)) if isinstance(olt_id, str) else olt_id
    total = db.scalar(
        select(func.count()).select_from(OltServicePort).where(
            OltServicePort.olt_device_id == olt_uuid
        )
    )
    latest = db.scalar(
        select(func.max(OltServicePort.last_imported_at)).where(
            OltServicePort.olt_device_id == olt_uuid
        )
    )
    by_source_rows = db.execute(
        select(OltServicePort.source, func.count())
        .where(OltServicePort.olt_device_id == olt_uuid)
        .group_by(OltServicePort.source)
        .order_by(OltServicePort.source)
    ).all()
    by_fsp_rows = db.execute(
        select(OltServicePort.fsp, func.count())
        .where(OltServicePort.olt_device_id == olt_uuid)
        .group_by(OltServicePort.fsp)
        .order_by(OltServicePort.fsp)
    ).all()
    return {
        "count": int(total or 0),
        "last_imported_at": latest,
        "by_source": [
            {"source": str(source or "unknown"), "count": int(count or 0)}
            for source, count in by_source_rows
        ],
        "by_fsp": [
            {"fsp": str(fsp or ""), "count": int(count or 0)}
            for fsp, count in by_fsp_rows
        ],
    }


def upsert_imported_service_port_from_readback(
    db: Session,
    *,
    olt: OLTDevice,
    ont: OntUnit,
    port: ServicePortEntry,
    source: str,
) -> None:
    """Record service-port state confirmed by live write readback."""
    observed = db.scalars(
        select(OltServicePort).where(
            OltServicePort.olt_device_id == olt.id,
            OltServicePort.port_index == port.index,
        )
    ).first()
    now = datetime.now(UTC)
    if observed is None:
        observed = OltServicePort(
            olt_device_id=olt.id,
            port_index=port.index,
            fsp=port.fsp,
            ont_id_on_olt=port.ont_id,
            vlan_id=port.vlan_id,
            gem_index=port.gem_index,
            source=source,
            last_imported_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(observed)

    observed.ont_unit_id = ont.id
    observed.fsp = port.fsp
    observed.ont_id_on_olt = port.ont_id
    observed.vlan_id = port.vlan_id
    observed.gem_index = port.gem_index
    observed.user_vlan = None
    observed.tag_transform = port.tag_transform or None
    observed.flow_type = port.flow_type or None
    observed.flow_para = port.flow_para or None
    observed.state = port.state or None
    observed.source = source
    observed.raw_entry = {
        "index": port.index,
        "fsp": port.fsp,
        "ont_id": port.ont_id,
        "vlan_id": port.vlan_id,
        "gem_index": port.gem_index,
        "flow_type": port.flow_type,
        "flow_para": port.flow_para,
        "state": port.state,
        "tag_transform": port.tag_transform,
    }
    observed.last_imported_at = now
    observed.updated_at = now


def delete_imported_service_port(
    db: Session,
    *,
    olt_id: UUID | str,
    port_index: int,
) -> bool:
    """Delete one imported service-port row after confirmed OLT deletion."""
    olt_uuid = UUID(str(olt_id)) if isinstance(olt_id, str) else olt_id
    observed = db.scalars(
        select(OltServicePort).where(
            OltServicePort.olt_device_id == olt_uuid,
            OltServicePort.port_index == port_index,
        )
    ).first()
    if observed is None:
        return False
    db.delete(observed)
    return True
