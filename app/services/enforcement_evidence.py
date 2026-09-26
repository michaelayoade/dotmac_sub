"""Enforcement application evidence: the ONE writer (ADR 0017).

``access.enforcement_evidence`` owns the ``EnforcementApplication`` observation:
one current-state row per (subscription, NAS device, effect) recording what an
enforcement attempt actually did on a device. ``access.session_enforcement``
(``app/services/enforcement.py``) performs the attempts and hands each final
per-NAS ``EnforcementOutcome`` here.

Transaction mode ``out_of_band_evidence``: the write runs on its own
``db_session_adapter.create_session()`` unit of work so the evidence of an
irreversible device effect survives the caller's rollback. It never joins the
caller's transaction, emits no domain event, and never raises into the caller
(except a Celery soft time limit, which must reach the task).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.logging import sanitize_exception
from app.models.enforcement_application import (
    ENFORCEMENT_APPLICATION_DETAIL_MAX_LENGTH,
    EnforcementApplication,
    EnforcementEffect,
    EnforcementFailureClass,
    EnforcementOutcomeValue,
    EnforcementPath,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.nas.enforcement_failure import classify_enforcement_failure

if TYPE_CHECKING:
    from sqlalchemy import Table

logger = logging.getLogger(__name__)

__all__ = ["EnforcementOutcome", "record_enforcement_application"]


@dataclass(frozen=True)
class EnforcementOutcome:
    """Typed result of one enforcement attempt on one NAS (ADR-0017 §4).

    Replaces the historical bare ``bool`` return from the per-NAS enforcement
    helpers, which conflated "not applicable" (not MikroTik, no credentials,
    feature disabled) with a real transport failure. This is a value object
    only: producing one never writes anything — see
    ``record_enforcement_application`` for the sole writer.
    """

    outcome: EnforcementOutcomeValue
    failure_class: EnforcementFailureClass | None
    path: EnforcementPath | None
    detail: str | None

    @classmethod
    def applied(cls, path: EnforcementPath) -> EnforcementOutcome:
        return cls(
            outcome=EnforcementOutcomeValue.applied,
            failure_class=None,
            path=path,
            detail=None,
        )

    @classmethod
    def not_applicable(cls, detail: str | None = None) -> EnforcementOutcome:
        return cls(
            outcome=EnforcementOutcomeValue.not_applicable,
            failure_class=None,
            path=None,
            detail=detail,
        )

    @classmethod
    def failed_from(
        cls, exc: BaseException, path: EnforcementPath
    ) -> EnforcementOutcome:
        failure_class, detail = classify_enforcement_failure(exc)
        return cls(
            outcome=EnforcementOutcomeValue.failed,
            failure_class=failure_class,
            path=path,
            detail=detail[:ENFORCEMENT_APPLICATION_DETAIL_MAX_LENGTH],
        )


def _is_task_time_limit(exc: BaseException) -> bool:
    try:
        from billiard.exceptions import SoftTimeLimitExceeded
    except ImportError:  # pragma: no cover - billiard ships with Celery
        return False
    return isinstance(exc, SoftTimeLimitExceeded)


def record_enforcement_application(
    *,
    subscription_id: UUID,
    nas_device_id: UUID,
    effect: EnforcementEffect,
    outcome: EnforcementOutcome,
) -> None:
    """Out-of-band writer for enforcement evidence (ADR-0017 §2, §3, §7).

    The sole writer of ``EnforcementApplication``. Opens its own short
    session via ``db_session_adapter.create_session()`` (never the caller's
    session — the caller may hold ``SELECT ... FOR UPDATE`` on the
    subscription row, and this upsert must survive that transaction's
    rollback), upserts the current-state row for
    ``(subscription_id, nas_device_id, effect)``, commits, and closes.

    Never raises: the device effect has already happened, so failing the
    caller here could re-trigger it. A write failure is logged at ``ERROR``
    (so it opens a GlitchTip issue) and swallowed.
    """
    session: Session | None = None
    try:
        # Inside the try: an unreachable database at session-open time must
        # be swallowed and logged like any other write failure (ADR-0017 §7).
        session = db_session_adapter.create_session()
        now = datetime.now(UTC)
        table = cast("Table", EnforcementApplication.__table__)
        dialect_name = session.bind.dialect.name if session.bind is not None else ""
        is_postgresql = dialect_name == "postgresql"
        if is_postgresql:
            session.execute(text("SET LOCAL lock_timeout = '2s'"))

        failure_class_value = (
            outcome.failure_class.value if outcome.failure_class is not None else None
        )
        path_value = outcome.path.value if outcome.path is not None else None

        values: dict[str, Any] = {
            "id": uuid4(),
            "subscription_id": subscription_id,
            "nas_device_id": nas_device_id,
            "effect": effect.value,
            "outcome": outcome.outcome.value,
            "failure_class": failure_class_value,
            "path": path_value,
            "detail": outcome.detail,
            "last_attempt_at": now,
            "created_at": now,
            "updated_at": now,
        }
        set_: dict[str, Any] = {
            "outcome": outcome.outcome.value,
            "failure_class": failure_class_value,
            "path": path_value,
            "detail": outcome.detail,
            "last_attempt_at": now,
            "updated_at": now,
        }

        if outcome.outcome == EnforcementOutcomeValue.failed:
            values["attempt_count"] = 1
            values["first_failed_at"] = now
            values["last_success_at"] = None
            set_["attempt_count"] = table.c.attempt_count + 1
            set_["first_failed_at"] = func.coalesce(table.c.first_failed_at, now)
            set_["last_success_at"] = table.c.last_success_at
        elif outcome.outcome == EnforcementOutcomeValue.applied:
            values["attempt_count"] = 0
            values["first_failed_at"] = None
            values["last_success_at"] = now
            set_["attempt_count"] = 0
            set_["first_failed_at"] = None
            set_["last_success_at"] = now
        else:  # not_applicable
            # A not-applicable attempt is a fresh state, not a continuation
            # of an earlier failure streak: clear the failure counters so the
            # row never reads "not_applicable" with a live failure history.
            values["attempt_count"] = 0
            values["first_failed_at"] = None
            values["last_success_at"] = None
            set_["attempt_count"] = 0
            set_["first_failed_at"] = None
            set_["last_success_at"] = table.c.last_success_at

        if is_postgresql:
            pg_stmt = pg_insert(table).values(**values)
            pg_stmt = pg_stmt.on_conflict_do_update(
                index_elements=["subscription_id", "nas_device_id", "effect"],
                set_=set_,
            )
            session.execute(pg_stmt)
        else:
            sqlite_stmt = sqlite_insert(table).values(**values)
            sqlite_stmt = sqlite_stmt.on_conflict_do_update(
                index_elements=["subscription_id", "nas_device_id", "effect"],
                set_=set_,
            )
            session.execute(sqlite_stmt)
        session.commit()
    except Exception as exc:
        if _is_task_time_limit(exc):
            # A Celery soft time limit must reach the task; swallowing it here
            # would let the task run past its budget (ADR-0017 section 7 covers
            # write failures, not task cancellation).
            raise
        logger.error(
            "enforcement_application_record_failed",
            extra={
                "event": "enforcement_application_record_failed",
                "subscription_id": str(subscription_id),
                "nas_device_id": str(nas_device_id),
                "effect": effect.value,
                "detail": sanitize_exception(exc),
            },
        )
        if session is not None:
            try:
                session.rollback()
            except Exception:
                pass
    finally:
        if session is not None:
            session.close()
