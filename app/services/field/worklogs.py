"""Atomic worklog submission for authenticated native field actors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.field_worklog import FieldWorkLog
from app.schemas.field import FieldWorkLogRead, FieldWorkLogResult
from app.services.events.owner_outputs import OwnerOutputEnvelope, stage_owner_output
from app.services.events.types import EventType
from app.services.field.execution_contracts import FieldJobQuery, SubmitFieldWorkLogs
from app.services.field.work_order_access import (
    FieldAccessError,
    FieldActor,
    FieldWorkOrderScope,
    ResolveFieldActor,
    require_work_order,
    resolve_field_actor,
)
from app.services.owner_commands import OwnerCommandDefinition, execute_owner_command

_MAX_DURATION_HOURS = 16
_BACKDATED_FLAG_DAYS = 7


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def actor_worklogs(db: Session, actor: FieldActor):
    # SystemUser is always present for both actor kinds. Never compare a
    # nullable legacy Person FK to NULL to identify a vendor's timer.
    predicate = FieldWorkLog.system_user_id == actor.system_user_id
    if actor.person_id is not None:
        predicate = or_(predicate, FieldWorkLog.person_id == actor.person_id)
    return db.query(FieldWorkLog).filter(
        predicate,
        FieldWorkLog.is_active.is_(True),
    )


def _serialize(log: FieldWorkLog) -> FieldWorkLogRead:
    return FieldWorkLogRead(
        id=log.id,
        person_id=log.person_id,
        start_at=log.start_at,
        end_at=log.end_at,
        minutes=log.minutes,
        notes=log.notes,
    )


def _error(code: str, message: str) -> FieldAccessError:
    return FieldAccessError(
        code=f"operations.field_worklogs.{code}", message=message, retryable=False
    )


class FieldWorkLogs:
    @staticmethod
    def list_for_job(db: Session, query: FieldJobQuery) -> tuple[FieldWorkLogRead, ...]:
        actor = resolve_field_actor(
            db, ResolveFieldActor(query.requester_system_user_id)
        )
        row = require_work_order(db, FieldWorkOrderScope(actor, query.public_id))
        logs = (
            db.query(FieldWorkLog)
            .filter(
                FieldWorkLog.work_order_mirror_id == row.id,
                FieldWorkLog.is_active.is_(True),
            )
            .order_by(FieldWorkLog.start_at.asc())
            .all()
        )
        return tuple(_serialize(log) for log in logs)

    @staticmethod
    def submit(
        db: Session, command: SubmitFieldWorkLogs
    ) -> tuple[FieldWorkLogResult, ...]:
        def operation() -> tuple[FieldWorkLogResult, ...]:
            # Serialize all timers for one actor, even on distinct work orders.
            from app.models.system_user import SystemUser

            db.query(SystemUser).filter(
                SystemUser.id == command.requester_system_user_id
            ).with_for_update().one_or_none()
            actor = resolve_field_actor(
                db, ResolveFieldActor(command.requester_system_user_id)
            )
            row = require_work_order(
                db, FieldWorkOrderScope(actor, command.public_id, lock=True)
            )
            now = datetime.now(UTC)
            results: list[FieldWorkLogResult] = []
            if not command.entries or len(command.entries) > 50:
                raise _error("invalid_request", "Submit between one and fifty worklogs")
            for entry in command.entries:
                start = _as_utc(entry.start_at)
                end = _as_utc(entry.end_at) if entry.end_at else None
                if end is not None and (
                    end <= start or end - start > timedelta(hours=_MAX_DURATION_HOURS)
                ):
                    raise _error(
                        "invalid_request",
                        "Worklog duration must be positive and at most sixteen hours",
                    )
                query = actor_worklogs(db, actor)
                duplicate = (
                    query.filter(
                        FieldWorkLog.client_ref == entry.client_ref
                    ).one_or_none()
                    if entry.client_ref
                    else query.filter(
                        FieldWorkLog.work_order_mirror_id == row.id,
                        FieldWorkLog.start_at == start,
                    ).first()
                )
                if duplicate is not None:
                    if (
                        duplicate.system_user_id != actor.system_user_id
                        or duplicate.work_order_mirror_id != row.id
                        or _as_utc(duplicate.start_at) != start
                        or (_as_utc(duplicate.end_at) if duplicate.end_at else None)
                        != end
                        or duplicate.notes != entry.notes
                    ):
                        raise _error(
                            "idempotency_conflict",
                            "Worklog request identity was reused with different details",
                        )
                    results.append(
                        FieldWorkLogResult(
                            worklog=_serialize(duplicate),
                            duplicate=True,
                            backdated=False,
                        )
                    )
                    continue
                candidates = query.filter(
                    or_(FieldWorkLog.end_at.is_(None), FieldWorkLog.end_at > start)
                ).all()
                for log in candidates:
                    log_start = _as_utc(log.start_at)
                    log_end = _as_utc(log.end_at) if log.end_at else None
                    if (end is None and log_end is None) or (
                        (end is None or log_start < end)
                        and (log_end is None or log_end > start)
                    ):
                        raise _error("conflict", "Worklog overlaps an existing entry")
                log = FieldWorkLog(
                    work_order_mirror_id=row.id,
                    author_technician_id=actor.technician_id,
                    author_vendor_user_id=actor.vendor_user_id,
                    person_id=actor.person_id,
                    system_user_id=actor.system_user_id,
                    start_at=start,
                    end_at=end,
                    minutes=max(0, int((end - start).total_seconds() // 60))
                    if end
                    else 0,
                    notes=entry.notes,
                    client_ref=entry.client_ref,
                )
                db.add(log)
                db.flush()
                results.append(
                    FieldWorkLogResult(
                        worklog=_serialize(log),
                        duplicate=False,
                        backdated=now - start > timedelta(days=_BACKDATED_FLAG_DAYS),
                    )
                )
            new_ids = tuple(item.worklog.id for item in results if not item.duplicate)
            if new_ids:
                stage_owner_output(
                    db,
                    OwnerOutputEnvelope(
                        event_type=EventType.field_worklogs_submitted,
                        producer_owner="operations.field_worklogs",
                        source_kind="field_worklog_submission",
                        source_id=command.context.command_id,
                    ),
                    {
                        "work_order_id": str(row.id),
                        "work_order_public_id": row.public_id,
                        "system_user_id": str(actor.system_user_id),
                        "worklog_ids": [str(item) for item in new_ids],
                    },
                    context=command.context,
                )
            return tuple(results)

        return execute_owner_command(
            db,
            definition=OwnerCommandDefinition(
                owner="operations.field_worklogs",
                concern="native field worklog submission",
                name="submit_field_worklogs",
            ),
            context=command.context,
            operation=operation,
        )


field_worklogs = FieldWorkLogs()
