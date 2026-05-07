"""Report ONT registrations that have no observed OLT service-port binding."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OltOntRegistration, OltServicePort


@dataclass(frozen=True)
class MissingServicePort:
    olt_id: str
    fsp: str
    ont_id_on_olt: int
    serial_number: str | None
    line_profile_id: int | None
    service_profile_id: int | None
    description: str | None


def find_missing_service_ports(
    db: Session,
    *,
    olt_id: str | None = None,
) -> list[MissingServicePort]:
    """Return active ONT registrations with no imported observed service-port."""
    reg_stmt = select(OltOntRegistration).where(OltOntRegistration.is_active.is_(True))
    if olt_id:
        reg_stmt = reg_stmt.where(OltOntRegistration.olt_id == olt_id)

    registrations = list(db.scalars(reg_stmt).all())
    port_stmt = select(
        OltServicePort.olt_device_id,
        OltServicePort.fsp,
        OltServicePort.ont_id_on_olt,
    )
    if olt_id:
        port_stmt = port_stmt.where(OltServicePort.olt_device_id == olt_id)
    observed = {
        (str(row[0]), str(row[1] or ""), int(row[2]))
        for row in db.execute(port_stmt).all()
        if row[0] is not None and row[1] and row[2] is not None
    }

    missing: list[MissingServicePort] = []
    for registration in registrations:
        key = (
            str(registration.olt_id),
            str(registration.fsp or ""),
            int(registration.ont_id_on_olt),
        )
        if key in observed:
            continue
        missing.append(
            MissingServicePort(
                olt_id=str(registration.olt_id),
                fsp=str(registration.fsp or ""),
                ont_id_on_olt=int(registration.ont_id_on_olt),
                serial_number=registration.serial_number,
                line_profile_id=registration.line_profile_id,
                service_profile_id=registration.service_profile_id,
                description=registration.description,
            )
        )
    return missing
