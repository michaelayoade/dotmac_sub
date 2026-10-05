"""Fast unit evidence for scheduled-rule transaction and replay behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.automation import (
    AutomationRule,
    AutomationRuleVersion,
    AutomationScheduledRun,
)
from app.services import automation_runtime, automation_scheduled
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import CommandContext

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _published_rule(db: Session) -> UUID:
    rule = AutomationRule(
        tenant_id=OPERATOR_TENANT_ID,
        key=f"scheduled-{uuid4()}",
        name="Scheduled transaction fixture",
        trigger_key="operations.project.scheduled",
        trigger_keys=["operations.project.scheduled"],
        status="published",
        created_by="pytest",
    )
    db.add(rule)
    db.flush()
    version = AutomationRuleVersion(
        tenant_id=OPERATOR_TENANT_ID,
        rule_id=rule.id,
        version=1,
        trigger_schema_version=1,
        trigger_schema_versions={"operations.project.scheduled": 1},
        conditions=[],
        actions=[],
        schedule={"type": "interval", "interval_seconds": 7200, "timezone": "UTC"},
        content_sha256="0" * 64,
        created_by="pytest",
        published_by="pytest",
        published_at=NOW,
    )
    db.add(version)
    db.flush()
    rule.active_version_id = version.id
    version_id = version.id
    db.commit()
    return version_id


def _command() -> automation_scheduled.EnqueueScheduledAutomationCommand:
    return automation_scheduled.EnqueueScheduledAutomationCommand(
        now=NOW,
        context=CommandContext.system(
            actor="pytest-scheduler",
            scope="automation:runtime",
            reason="Verify atomic scheduled delivery",
        ),
    )


def test_due_slot_is_committed_and_replay_emits_no_more_work(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    version_id = _published_rule(db_session)
    target_id = uuid4()
    payloads: list[dict[str, object]] = []
    monkeypatch.setattr(
        automation_scheduled,
        "targets_for",
        lambda db, adapter_key: [
            automation_scheduled.ScheduledAutomationTarget(
                entity_id=target_id,
                payload={"project_id": str(target_id), "status": "open"},
            )
        ],
    )

    def capture_event(db, event_type, payload, **kwargs):
        assert kwargs["dispatch_after_commit"] is False
        payloads.append(payload)

    monkeypatch.setattr(automation_scheduled, "emit_event", capture_event)
    first = automation_scheduled.enqueue_scheduled_events(
        db_session, command=_command()
    )
    assert first.rules_claimed == first.events_emitted == 1
    assert not db_session.in_transaction()
    assert payloads[0]["automation_rule_version_id"] == str(version_id)
    assert payloads[0]["tenant_id"] == str(OPERATOR_TENANT_ID)

    replay = automation_scheduled.enqueue_scheduled_events(
        db_session, command=_command()
    )
    assert replay.rules_claimed == replay.events_emitted == 0
    assert len(payloads) == 1
    assert (
        db_session.scalar(select(func.count()).select_from(AutomationScheduledRun)) == 1
    )


def test_event_failure_rolls_back_slot_so_the_same_work_can_retry(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _published_rule(db_session)
    target_id = uuid4()
    monkeypatch.setattr(
        automation_scheduled,
        "targets_for",
        lambda db, adapter_key: [
            automation_scheduled.ScheduledAutomationTarget(
                entity_id=target_id, payload={"project_id": str(target_id)}
            )
        ],
    )

    def fail_event(*args, **kwargs):
        raise RuntimeError("outbox write failed")

    monkeypatch.setattr(automation_scheduled, "emit_event", fail_event)
    with pytest.raises(RuntimeError, match="outbox write failed"):
        automation_scheduled.enqueue_scheduled_events(db_session, command=_command())
    assert (
        db_session.scalar(select(func.count()).select_from(AutomationScheduledRun)) == 0
    )
    db_session.rollback()

    monkeypatch.setattr(
        automation_scheduled, "emit_event", lambda *args, **kwargs: None
    )
    retry = automation_scheduled.enqueue_scheduled_events(
        db_session, command=_command()
    )
    assert retry.rules_claimed == retry.events_emitted == 1


def test_two_hour_interval_does_not_turn_into_an_hourly_schedule() -> None:
    schedule = {"type": "interval", "interval_seconds": 7200, "timezone": "UTC"}
    assert automation_scheduled._slot_for(
        schedule, NOW
    ) == automation_scheduled._slot_for(schedule, NOW + timedelta(hours=1))
    assert automation_scheduled._slot_for(
        schedule, NOW
    ) != automation_scheduled._slot_for(schedule, NOW + timedelta(hours=2))


def test_scheduled_event_prepares_only_its_claimed_rule_version(
    db_session: Session,
) -> None:
    intended_version_id = _published_rule(db_session)
    _published_rule(db_session)
    target_id = uuid4()
    runs = automation_runtime.prepare_event_runs(
        db_session,
        automation_runtime.PrepareAutomationEventCommand(
            event=automation_runtime.AutomationEventEnvelope(
                event_id=uuid4(),
                event_type="operations.project.scheduled",
                trigger_key="operations.project.scheduled",
                tenant_id=OPERATOR_TENANT_ID,
                target_type="operations.project",
                target_id=target_id,
                occurred_at=NOW,
                payload={"project_id": str(target_id), "status": "open"},
                scheduled_rule_version_id=intended_version_id,
            ),
            context=_command().context,
        ),
    )
    assert len(runs) == 1
    assert runs[0].rule_version_id == intended_version_id
