"""Low-priority, bounded OLT observation refresh.

Refreshes only the OLT half of ``OntObservation`` for ONTs whose last genuine
OLT observation is older than a configurable age. It is read-only towards the
device: it never plans or applies an action and never changes ``sync_status``
or any desired state. The only write is the observation fact itself, through
the same ``upsert_ont_observation`` the reconcile pass uses.

Bounds, all fail-closed:

* at most ``per_olt_limit`` ONTs per OLT per cycle (oldest observation first);
* the whole OLT is skipped while another operation targeting that OLT, or an
  ONT behind it, is pending or running;
* only reviewed, unexpired admitted ONTs are read, and each is re-authorised
  under its row lock before the observation is stored;
* a busy ONT row (``NOWAIT`` lock conflict) is skipped, never waited for.

A global per-OLT session limiter is out of scope here (PR-A adds it).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.network import DeviceStatus, OLTDevice, OntUnit
from app.models.network_operation import (
    NetworkOperation,
    NetworkOperationStatus,
    NetworkOperationTargetType,
)
from app.models.ont_observation import OntObservation

from .adapters import desired_from_ont_unit, upsert_ont_observation
from .candidates import restrict_to_reconcile_candidates
from .locking import LockConflict, acquire_reconcile_lock
from .readers import read_olt_state
from .state import OntObservedState

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_HOURS = 6
DEFAULT_PER_OLT_LIMIT = 5

_ACTIVE_OPERATION_STATUSES = (
    NetworkOperationStatus.pending,
    NetworkOperationStatus.running,
)


class ObservationRefreshResult(StrEnum):
    refreshed = "refreshed"
    read_failed = "read_failed"
    busy = "busy"
    not_eligible = "not_eligible"


@dataclass(frozen=True, slots=True)
class ObservationRefreshPolicy:
    max_age: timedelta
    per_olt_limit: int


def load_refresh_policy(db: Session) -> ObservationRefreshPolicy:
    """Operator-configured age and per-OLT bound (settings_spec defaults 6 h / 5)."""

    from app.models.domain_settings import SettingDomain
    from app.services.settings_spec import resolve_integer

    hours = resolve_integer(
        db, SettingDomain.network_monitoring, "ont_olt_observation_max_age_hours"
    )
    per_olt = resolve_integer(
        db, SettingDomain.network_monitoring, "ont_olt_observation_refresh_per_olt"
    )
    return ObservationRefreshPolicy(
        max_age=timedelta(hours=max(1, hours or DEFAULT_MAX_AGE_HOURS)),
        per_olt_limit=max(1, min(20, per_olt or DEFAULT_PER_OLT_LIMIT)),
    )


def refreshable_olt_ids(db: Session) -> list[uuid.UUID]:
    return list(
        db.scalars(
            select(OLTDevice.id).where(
                OLTDevice.is_active.is_(True),
                OLTDevice.status == DeviceStatus.active,
                OLTDevice.uisp_device_id.is_(None),
            )
        )
    )


def olt_observation_age(
    db: Session, ont_unit_id: uuid.UUID, *, now: datetime | None = None
) -> tuple[datetime | None, float | None]:
    """The ONT's last genuine OLT observation time and its age in hours."""

    observed_at = db.scalar(
        select(OntObservation.olt_observed_at).where(
            OntObservation.ont_unit_id == ont_unit_id
        )
    )
    if observed_at is None:
        return None, None
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    seconds = max(((now or datetime.now(UTC)) - observed_at).total_seconds(), 0)
    return observed_at, round(seconds / 3600, 1)


def olt_has_active_operation(db: Session, olt_id: uuid.UUID) -> bool:
    """Whether a pending/running operation targets this OLT or an ONT on it."""

    ont_ids = select(OntUnit.id).where(OntUnit.olt_device_id == olt_id)
    found = db.scalars(
        select(NetworkOperation.id)
        .where(NetworkOperation.status.in_(_ACTIVE_OPERATION_STATUSES))
        .where(
            or_(
                (NetworkOperation.target_type == NetworkOperationTargetType.olt)
                & (NetworkOperation.target_id == olt_id),
                (NetworkOperation.target_type == NetworkOperationTargetType.ont)
                & (NetworkOperation.target_id.in_(ont_ids)),
            )
        )
        .limit(1)
    ).first()
    return found is not None


def stale_observation_ont_ids(
    db: Session,
    olt_id: uuid.UUID,
    *,
    older_than: datetime,
    limit: int,
) -> list[uuid.UUID]:
    """Admitted ONTs on one OLT with the oldest stale OLT observation first."""

    from app.services.network.ont_reconcile_eligibility import admitted_ont_ids

    admitted = admitted_ont_ids(db)
    if not admitted:
        return []
    stmt = restrict_to_reconcile_candidates(
        select(OntUnit.id).join(
            OntObservation, OntObservation.ont_unit_id == OntUnit.id
        )
    )
    stmt = (
        stmt.where(OntUnit.olt_device_id == olt_id)
        .where(OntUnit.id.in_(admitted))
        .where(
            or_(
                OntObservation.olt_observed_at.is_(None),
                OntObservation.olt_observed_at < older_than,
            )
        )
        .order_by(OntObservation.olt_observed_at.asc().nullsfirst(), OntUnit.id)
        .limit(max(1, int(limit)))
    )
    return list(db.scalars(stmt))


def _load_cached_state(db: Session, ont_id: uuid.UUID) -> OntObservedState | None:
    from .adapters import observed_from_ont_observation

    row = db.scalars(
        select(OntObservation).where(OntObservation.ont_unit_id == ont_id)
    ).first()
    return observed_from_ont_observation(row)


def refresh_one_ont_observation(
    db: Session,
    ont_id: uuid.UUID,
    adapter: Any,
    *,
    now: datetime | None = None,
    read_fn: Callable[..., Any] = read_olt_state,
) -> ObservationRefreshResult:
    """Refresh the OLT half of one ONT's stored observation.

    Flush-only: the caller (scheduled task adapter) completes the transaction.
    ``read_failed`` wrote nothing; ``busy``/``not_eligible`` need a rollback.
    """

    current = now or datetime.now(UTC)
    ont = db.get(OntUnit, ont_id)
    cached = _load_cached_state(db, ont_id)
    if ont is None or cached is None:
        return ObservationRefreshResult.not_eligible
    try:
        read = read_fn(adapter, desired_from_ont_unit(db, ont))
    except Exception:  # noqa: BLE001 - one device must not stop the cycle
        logger.exception(
            "olt_observation_refresh_read_failed", extra={"ont_id": str(ont_id)}
        )
        return ObservationRefreshResult.read_failed
    if read.status == "unavailable" or read.observed is None:
        return ObservationRefreshResult.read_failed
    from app.services.network.ont_reconcile_eligibility import (
        eligibility_under_lock,
    )

    try:
        with acquire_reconcile_lock(db, ont_id, nowait=True) as locked:
            if not eligibility_under_lock(db, ont_id).eligible:
                return ObservationRefreshResult.not_eligible
            prior_reconciled_at = cached.last_reconciled_at
            prior_duration = cached.last_reconcile_duration_ms
            row = upsert_ont_observation(
                db,
                locked.id,
                replace(cached, last_reconciled_at=current, olt=read.observed),
                observed_surfaces=frozenset({"olt"}),
                olt_read_status=read.status,
            )
            # Only the OLT half is refreshed: keep the full-reconcile
            # bookkeeping (ordering and "last observation") untouched.
            row.last_reconciled_at = prior_reconciled_at
            row.last_reconcile_duration_ms = prior_duration
            return ObservationRefreshResult.refreshed
    except LockConflict:
        return ObservationRefreshResult.busy


__all__ = [
    "ObservationRefreshPolicy",
    "ObservationRefreshResult",
    "load_refresh_policy",
    "olt_has_active_operation",
    "olt_observation_age",
    "refresh_one_ont_observation",
    "refreshable_olt_ids",
    "stale_observation_ont_ids",
]
