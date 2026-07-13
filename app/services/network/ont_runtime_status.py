"""Native Huawei ONT runtime-status readers and persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntUnit, OnuOfflineReason, OnuOnlineStatus
from app.services.network.ont_status import apply_olt_status_observation
from app.services.network.serial_utils import canonical, parse_ont_id_on_olt


@dataclass(frozen=True)
class OltStatusRefreshStats:
    olt_id: str
    observed: int
    online: int
    offline: int
    unmatched: int
    invalid: int


def _binary_run_state(run_state: str | None) -> OnuOnlineStatus:
    normalized = str(run_state or "").strip().lower()
    if normalized in {"online", "up", "active", "working"}:
        return OnuOnlineStatus.online
    if normalized in {"offline", "down", "inactive", "los"}:
        return OnuOnlineStatus.offline
    raise ValueError(f"Unsupported Huawei ONT run state: {run_state!r}")


def _ont_fsp(db: Session, ont: OntUnit) -> str:
    board = str(ont.board or "").strip()
    port = str(ont.port or "").strip()
    if board and port:
        return f"{board}/{port}"
    pon_port = getattr(ont, "pon_port", None)
    if pon_port is None and ont.pon_port_id is not None:
        from app.models.network import PonPort

        pon_port = db.get(PonPort, ont.pon_port_id)
    fsp = str(getattr(pon_port, "name", "") or "").strip()
    if fsp:
        return fsp
    raise ValueError("ONT has no Huawei F/S/P location")


def refresh_single_ont_status(db: Session, ont: OntUnit) -> OnuOnlineStatus:
    """Read one Huawei ONT directly and persist only parsed device evidence."""
    olt = getattr(ont, "olt_device", None)
    if olt is None and ont.olt_device_id is not None:
        olt = db.get(OLTDevice, ont.olt_device_id)
    if olt is None:
        raise ValueError("ONT has no OLT")
    if str(olt.vendor or "").strip().lower() != "huawei":
        raise ValueError("Direct status refresh is only supported for Huawei ONTs")

    ont_id = parse_ont_id_on_olt(ont.external_id)
    if ont_id is None:
        raise ValueError("ONT has no valid OLT-local ID")

    from app.services.network.olt_ssh_ont.status import get_ont_status

    ok, message, observed = get_ont_status(olt, _ont_fsp(db, ont), ont_id)
    if not ok or observed is None:
        raise RuntimeError(message)
    status = _binary_run_state(observed.run_state)
    apply_olt_status_observation(
        ont,
        status,
        OnuOfflineReason.unknown if status == OnuOnlineStatus.offline else None,
    )
    db.flush()
    return status


def refresh_huawei_olt_status(
    db: Session,
    olt: OLTDevice,
    *,
    now: datetime | None = None,
) -> OltStatusRefreshStats:
    """Read all ONTs in one OLT command and persist matched observations.

    An empty or unparsable response is a poll failure, never evidence that all
    ONTs are offline. Inventory rows absent from the response retain their last
    confirmed binary state and are retried by the next scheduled sweep.
    """
    from app.services.network.olt_ssh_ont.status import get_registered_ont_serials

    onts = list(
        db.scalars(
            select(OntUnit).where(
                OntUnit.olt_device_id == olt.id,
                OntUnit.is_active.is_(True),
            )
        ).all()
    )
    ok, message, entries = get_registered_ont_serials(olt)
    if not ok:
        raise RuntimeError(message)
    if onts and not entries:
        raise RuntimeError(
            "Huawei ONT summary returned no parseable rows for a populated OLT"
        )

    by_serial = {
        canonical(serial): ont
        for ont in onts
        for serial in (ont.serial_number, ont.vendor_serial_number)
        if canonical(serial)
    }
    observed_at = now or datetime.now(UTC)
    online = offline = unmatched = invalid = 0
    matched_ids: set[object] = set()
    for entry in entries:
        ont = by_serial.get(canonical(entry.real_serial))
        if ont is None or ont.id in matched_ids:
            unmatched += 1
            continue
        try:
            status = _binary_run_state(entry.run_state)
        except ValueError:
            invalid += 1
            continue
        apply_olt_status_observation(
            ont,
            status,
            OnuOfflineReason.unknown if status == OnuOnlineStatus.offline else None,
            now=observed_at,
        )
        matched_ids.add(ont.id)
        if status == OnuOnlineStatus.online:
            online += 1
        else:
            offline += 1

    db.flush()
    return OltStatusRefreshStats(
        olt_id=str(olt.id),
        observed=len(matched_ids),
        online=online,
        offline=offline,
        unmatched=unmatched,
        invalid=invalid,
    )
