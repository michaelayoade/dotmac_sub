"""CoA outcome evidence: the ONE writer of ``nas_coa_outcome_buckets`` (ADR 0018).

``network.coa_outcome_evidence`` records, per NAS and UTC hour, how many RADIUS
CoA attempts ended in each disposition. Transaction mode
``out_of_band_evidence``: the write runs on its own
``db_session_adapter.create_session()`` unit of work so the evidence of an
irreversible device effect survives the caller's rollback and never joins, or
waits on, the enforcement caller's transaction. It never raises into the caller
(except a Celery soft time limit) and failures are counted, not hidden.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.logging import sanitize_exception
from app.models.router_health import (
    COA_OUTCOME_DISPOSITIONS,
    COA_OUTCOME_KINDS,
    NasCoaOutcomeBucket,
)
from app.services import observability
from app.services.db_session_adapter import db_session_adapter
from app.services.router_health.contracts import CoaKind

if TYPE_CHECKING:
    from sqlalchemy import Table

logger = logging.getLogger(__name__)

__all__ = ["record_coa_outcome"]

STATEMENT_TIMEOUT_MS = 500
LOCK_TIMEOUT_MS = 200
#: Circuit breaker: after this many consecutive failures, skip writes briefly so a
#: database problem costs the enforcement path at most one bounded wait per
#: window rather than one per CoA.
BREAKER_FAILURES = 3
BREAKER_SECONDS = 60.0

_breaker_lock = threading.Lock()
_consecutive_failures = 0
_breaker_open_until = 0.0


def reset_breaker() -> None:
    """Test hook: close the circuit breaker."""
    global _consecutive_failures, _breaker_open_until
    with _breaker_lock:
        _consecutive_failures = 0
        _breaker_open_until = 0.0


def _breaker_open() -> bool:
    with _breaker_lock:
        return time.monotonic() < _breaker_open_until


def _note_result(ok: bool) -> None:
    global _consecutive_failures, _breaker_open_until
    with _breaker_lock:
        if ok:
            _consecutive_failures = 0
            return
        _consecutive_failures += 1
        if _consecutive_failures >= BREAKER_FAILURES:
            _breaker_open_until = time.monotonic() + BREAKER_SECONDS
            _consecutive_failures = 0


def is_task_time_limit(exc: BaseException) -> bool:
    try:
        from billiard.exceptions import SoftTimeLimitExceeded
    except ImportError:  # pragma: no cover - billiard ships with Celery
        return False
    return isinstance(exc, SoftTimeLimitExceeded)


def _hour_start(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def record_coa_outcome(
    nas_device_id: UUID,
    kind: CoaKind | str,
    disposition: str,
    *,
    now: datetime | None = None,
) -> None:
    """Count one CoA outcome for ``nas_device_id`` in the current UTC hour.

    Out-of-band, bounded (500 ms statement / 200 ms lock timeout), never raises.
    """
    kind_value = kind.value if isinstance(kind, CoaKind) else str(kind)
    disposition_value = str(getattr(disposition, "value", disposition))
    if kind_value not in COA_OUTCOME_KINDS or (
        disposition_value not in COA_OUTCOME_DISPOSITIONS
    ):
        logger.error(
            "coa_outcome_record_invalid",
            extra={"event": "coa_outcome_record_invalid", "kind": kind_value},
        )
        return
    observability.record_metric(
        domain="coa", signal=kind_value, status=disposition_value
    )
    if _breaker_open():
        observability.record_metric(
            domain="coa_evidence", signal="write", status="circuit_open"
        )
        return
    session: Any = None
    try:
        session = db_session_adapter.create_session()
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        table = cast("Table", NasCoaOutcomeBucket.__table__)
        is_postgresql = (
            session.bind is not None and session.bind.dialect.name == "postgresql"
        )
        if is_postgresql:
            session.execute(
                text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'")
            )
            session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'"))
        values: dict[str, Any] = {
            "id": uuid4(),
            "nas_device_id": nas_device_id,
            "kind": kind_value,
            "disposition": disposition_value,
            "bucket_start": _hour_start(moment),
            "count": 1,
            "last_at": moment,
        }
        insert_fn = pg_insert if is_postgresql else sqlite_insert
        statement = insert_fn(table).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["nas_device_id", "kind", "disposition", "bucket_start"],
            set_={"count": table.c.count + 1, "last_at": moment},
        )
        session.execute(statement)
        session.commit()
        _note_result(True)
    except Exception as exc:
        if is_task_time_limit(exc):
            raise
        _note_result(False)
        observability.record_metric(
            domain="coa_evidence", signal="write", status="write_failed"
        )
        logger.error(
            "coa_outcome_record_failed",
            extra={
                "event": "coa_outcome_record_failed",
                "nas_device_id": str(nas_device_id),
                "kind": kind_value,
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


def prune_coa_outcome_buckets(
    *, retention_days: int, now: datetime | None = None
) -> int:
    """Delete buckets older than ``retention_days`` on this writer's own session.

    Never raises (except a Celery soft time limit); returns rows deleted.
    """
    from datetime import timedelta

    from sqlalchemy import delete

    session: Any = None
    try:
        session = db_session_adapter.create_session()
        cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
        result = session.execute(
            delete(NasCoaOutcomeBucket).where(NasCoaOutcomeBucket.bucket_start < cutoff)
        )
        session.commit()
        return int(result.rowcount or 0)
    except Exception as exc:
        if is_task_time_limit(exc):
            raise
        observability.record_metric(
            domain="coa_evidence", signal="prune", status="write_failed"
        )
        logger.error(
            "coa_outcome_prune_failed",
            extra={
                "event": "coa_outcome_prune_failed",
                "detail": sanitize_exception(exc),
            },
        )
        if session is not None:
            try:
                session.rollback()
            except Exception:
                pass
        return 0
    finally:
        if session is not None:
            session.close()
