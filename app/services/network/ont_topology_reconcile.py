"""Reconcile ONT topology pointers from imported OLT registrations."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    OltOntRegistration,
    OntAssignment,
    OntUnit,
    PonPort,
)
from app.services.network.serial_utils import canonical as canonical_serial


@dataclass(frozen=True)
class OntPonPortRepair:
    serial_number: str
    olt_id: str
    registration_fsp: str
    current_fsp: str | None
    target_pon_port_id: str | None
    current_pon_port_id: str | None
    ont_unit_id: str
    assignment_ids: list[str] = field(default_factory=list)
    created_pon_port: bool = False
    changed: bool = False
    skipped_reason: str | None = None


@dataclass(frozen=True)
class OntPonPortReconcileResult:
    apply: bool
    candidates: list[OntPonPortRepair]
    updated: int = 0
    created_pon_ports: int = 0
    already_correct: int = 0
    missing_from_db: int = 0
    missing_from_registration: int = 0
    skipped: int = 0


def _parse_fsp(fsp: str | None) -> tuple[str, str, str] | None:
    parts = [part.strip() for part in str(fsp or "").split("/")]
    if len(parts) != 3 or not all(part != "" for part in parts):
        return None
    return parts[0], parts[1], parts[2]


def _location_from_fsp(fsp: str | None) -> tuple[str | None, str | None]:
    parsed = _parse_fsp(fsp)
    if parsed is None:
        return None, None
    frame, slot, port = parsed
    return f"{frame}/{slot}", port


def _current_fsp(db: Session, ont: OntUnit) -> str | None:
    pon_port = getattr(ont, "pon_port", None)
    if pon_port is None and ont.pon_port_id is not None:
        pon_port = db.get(PonPort, ont.pon_port_id)
    if pon_port is not None and pon_port.name:
        return str(pon_port.name)
    board = str(ont.board or "").strip()
    port = str(ont.port or "").strip()
    return f"{board}/{port}" if board and port else None


def _active_assignments(db: Session, ont: OntUnit) -> list[OntAssignment]:
    return list(
        db.scalars(
            select(OntAssignment)
            .where(OntAssignment.ont_unit_id == ont.id)
            .where(OntAssignment.active.is_(True))
        ).all()
    )


def _get_or_create_pon_port(
    db: Session,
    *,
    olt_id: object,
    fsp: str,
    apply: bool,
) -> tuple[PonPort | None, bool]:
    existing = db.scalars(
        select(PonPort)
        .where(PonPort.olt_id == olt_id)
        .where(PonPort.name == fsp)
        .limit(1)
    ).first()
    if existing is not None:
        return existing, False
    if not apply:
        return None, True
    parsed = _parse_fsp(fsp)
    port_number = int(parsed[2]) if parsed and parsed[2].isdigit() else None
    pon_port = PonPort(
        olt_id=olt_id,
        name=fsp,
        port_number=port_number,
        is_active=True,
        notes="Created by ONT registration topology reconciliation.",
    )
    db.add(pon_port)
    db.flush()
    return pon_port, True


def reconcile_ont_pon_ports_from_registrations(
    db: Session,
    *,
    olt_id: str | None = None,
    apply: bool = False,
) -> OntPonPortReconcileResult:
    """Align ONT/PonPort topology with active imported OLT registrations.

    The imported registration FSP is treated as the source of truth. Dry-run is
    the default: pass ``apply=True`` to update ``OntUnit.pon_port_id``, active
    ``OntAssignment.pon_port_id``, and ``OntUnit.board/port``.
    """
    reg_stmt = select(OltOntRegistration).where(OltOntRegistration.is_active.is_(True))
    ont_stmt = select(OntUnit).where(OntUnit.is_active.is_(True))
    if olt_id:
        reg_stmt = reg_stmt.where(OltOntRegistration.olt_id == olt_id)
        ont_stmt = ont_stmt.where(OntUnit.olt_device_id == olt_id)

    registrations = list(db.scalars(reg_stmt).all())
    onts = list(db.scalars(ont_stmt).all())

    ont_by_olt_serial: dict[tuple[str, str], OntUnit] = {}
    for ont in onts:
        if ont.olt_device_id is None:
            continue
        serial = canonical_serial(ont.serial_number)
        if not serial:
            continue
        ont_by_olt_serial.setdefault((str(ont.olt_device_id), serial), ont)

    registration_keys: set[tuple[str, str]] = set()
    candidates: list[OntPonPortRepair] = []
    missing_from_db = 0
    already_correct = 0
    skipped = 0
    updated = 0
    created_pon_ports = 0

    for registration in registrations:
        serial = canonical_serial(registration.serial_number)
        if not serial:
            skipped += 1
            candidates.append(
                OntPonPortRepair(
                    serial_number=str(registration.serial_number or ""),
                    olt_id=str(registration.olt_id),
                    registration_fsp=str(registration.fsp or ""),
                    current_fsp=None,
                    target_pon_port_id=None,
                    current_pon_port_id=None,
                    ont_unit_id="",
                    skipped_reason="registration has no serial number",
                )
            )
            continue
        key = (str(registration.olt_id), serial)
        registration_keys.add(key)
        # ``matched_ont`` holds the optional lookup result; ``ont`` rebinds
        # to the non-None value after the early continue so the rest of the
        # loop body keeps the original variable name without confusing the
        # earlier ``for ont in onts:`` loop type.
        matched_ont = ont_by_olt_serial.get(key)
        if matched_ont is None:
            missing_from_db += 1
            continue
        ont = matched_ont

        registration_fsp = str(registration.fsp or "").strip()
        board, port = _location_from_fsp(registration_fsp)
        if board is None or port is None:
            skipped += 1
            candidates.append(
                OntPonPortRepair(
                    serial_number=ont.serial_number,
                    olt_id=str(registration.olt_id),
                    registration_fsp=registration_fsp,
                    current_fsp=_current_fsp(db, ont),
                    target_pon_port_id=None,
                    current_pon_port_id=str(ont.pon_port_id) if ont.pon_port_id else None,
                    ont_unit_id=str(ont.id),
                    skipped_reason="registration FSP is invalid",
                )
            )
            continue

        target_pon_port, created = _get_or_create_pon_port(
            db,
            olt_id=registration.olt_id,
            fsp=registration_fsp,
            apply=apply,
        )
        assignments = _active_assignments(db, ont)
        assignment_ids = [str(assignment.id) for assignment in assignments]
        target_id = getattr(target_pon_port, "id", None)
        current_fsp = _current_fsp(db, ont)
        current_id = ont.pon_port_id

        needs_update = (
            current_fsp != registration_fsp
            or str(ont.board or "").strip() != board
            or str(ont.port or "").strip() != port
            or (target_id is not None and current_id != target_id)
            or any(
                target_id is not None and assignment.pon_port_id != target_id
                for assignment in assignments
            )
        )
        if not needs_update:
            already_correct += 1
            continue

        if apply:
            ont.board = board
            ont.port = port
            if target_pon_port is not None:
                ont.pon_port_id = target_pon_port.id
                for assignment in assignments:
                    assignment.pon_port_id = target_pon_port.id
            updated += 1
            if created:
                created_pon_ports += 1

        candidates.append(
            OntPonPortRepair(
                serial_number=ont.serial_number,
                olt_id=str(registration.olt_id),
                registration_fsp=registration_fsp,
                current_fsp=current_fsp,
                target_pon_port_id=str(target_id) if target_id else None,
                current_pon_port_id=str(current_id) if current_id else None,
                ont_unit_id=str(ont.id),
                assignment_ids=assignment_ids,
                created_pon_port=created,
                changed=apply,
            )
        )

    missing_from_registration = len(
        {
            key
            for key in ont_by_olt_serial
            if key not in registration_keys
        }
    )

    if apply:
        db.flush()

    return OntPonPortReconcileResult(
        apply=apply,
        candidates=candidates,
        updated=updated,
        created_pon_ports=created_pon_ports,
        already_correct=already_correct,
        missing_from_db=missing_from_db,
        missing_from_registration=missing_from_registration,
        skipped=skipped,
    )
