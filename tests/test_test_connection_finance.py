"""Finance workflows use the native temporary subscription grant owner."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.admin_alert import AdminNotification
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.event_store import EventStore
from app.models.notification import Notification, NotificationChannel
from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.subscriber import Subscriber
from app.models.test_connection import TestConnectionGrant as Grant
from app.models.test_connection_review import TestConnectionFinanceReview as Review
from app.schemas.test_connection import TestConnectionCreated as Evidence
from app.services import automation_rules
from app.services import test_connection as owner
from app.services.automation_contracts import AutomationOperator
from app.services.events.handlers.automation import AutomationEventHandler
from app.services.events.types import Event, EventType
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import CommandContext
from app.services.test_connection_finance import (
    NotifyTestConnectionFinanceCommand,
    notify_test_connection_finance,
)
from app.services.test_connection_finance import (
    TestConnectionFinanceError as FinanceError,
)
from tests.staff_identity_fixtures import add_bound_staff_user

NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def suppress_unrelated_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    # Retain real outbox records but isolate setup acknowledgements and network
    # consequences; drive the Finance workflow explicitly in these tests.
    monkeypatch.setattr(
        "app.services.events.dispatcher.run_after_commit", lambda *_: None
    )


@dataclass
class Clock:
    now: datetime = NOW


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now if tz is not None else clock.now.replace(tzinfo=None)

    monkeypatch.setattr(owner, "datetime", FixedDatetime)
    monkeypatch.setattr(
        "app.services.events.dispatcher.run_after_commit", lambda *_: None
    )
    return clock


def _context(scope: str) -> CommandContext:
    return CommandContext.system(
        actor="service:pytest-finance",
        scope=scope,
        reason="Verify Finance review",
        idempotency_key=str(uuid4()),
    )


def _create(
    db: Session,
    service: owner.ActivateTestConnectionCommand,
    clock: Clock,
    *,
    key: UUID | None = None,
) -> tuple[UUID, Event]:
    # The real activation owner retires the previous interval at its deadline.
    running = db.scalar(
        select(Grant).where(
            Grant.subscription_id == service.subscription_id, Grant.ended_at.is_(None)
        )
    )
    if running is not None:
        clock.now = max(clock.now, running.expires_at.replace(tzinfo=UTC))
    context = replace(
        service.context, command_id=key or uuid4(), correlation_id=uuid4()
    )
    context = replace(context, idempotency_key=str(context.command_id))
    db.commit()
    result = owner.activate_test_connection(
        db, command=replace(service, context=context)
    )
    row = db.scalar(
        select(EventStore).where(
            EventStore.event_type == EventType.test_connection_created.value,
            EventStore.payload["grant_id"].as_string() == str(result.grant_id),
        )
    )
    assert row is not None
    event = Event(
        event_type=EventType.test_connection_created,
        event_id=row.event_id,
        payload=dict(row.payload),
        occurred_at=clock.now,
    )
    db.commit()
    return result.grant_id, event


@dataclass(frozen=True)
class Finance:
    team_id: UUID
    version_id: UUID
    recipients: tuple[UUID, ...]
    emails: tuple[str, ...]


def _finance(db: Session) -> Finance:
    team = ServiceTeam(name=f"Finance {uuid4()}", is_active=True)
    db.add(team)
    db.flush()
    emails = tuple(f"finance-{uuid4()}@example.com" for _ in range(2))
    users = [add_bound_staff_user(db, email=email) for email in emails]
    for user, person in users:
        db.add(ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True))
    team_id, recipients = team.id, tuple(user.id for user, _ in users)
    db.commit()
    rule = automation_rules.create_rule(
        db,
        automation_rules.CreateAutomationRuleCommand(
            tenant_id=OPERATOR_TENANT_ID,
            key=f"billing.test_connection.review_{uuid4().hex}",
            name="Repeated Test Connection Finance review",
            description="More than five native Test Connections in seven days",
            trigger_key="billing.test_connection.created",
            conditions=(
                automation_rules.AutomationCondition(
                    field_key="count_7d",
                    operator=AutomationOperator.greater_than,
                    value=5,
                ),
            ),
            actions=(
                automation_rules.AutomationActionStep(
                    action_key="billing.test_connection.notify_finance",
                    inputs=(
                        automation_rules.AutomationActionValue(
                            key="service_team_id", value=team_id
                        ),
                    ),
                ),
            ),
            permission_keys=frozenset({"*"}),
            context=_context("automation:rule:create"),
        ),
    )
    published = automation_rules.publish_rule(
        db,
        automation_rules.PublishAutomationRuleCommand(
            tenant_id=OPERATOR_TENANT_ID,
            rule_id=rule.rule_id,
            permission_keys=frozenset({"*"}),
            context=_context("automation:rule:publish"),
        ),
    )
    assert published.version_id is not None
    return Finance(team_id, published.version_id, recipients, emails)


def _notify(
    finance: Finance, grant_id: UUID, event: Event
) -> NotifyTestConnectionFinanceCommand:
    return NotifyTestConnectionFinanceCommand(
        context=_context("automation:runtime"),
        tenant_id=OPERATOR_TENANT_ID,
        event_id=event.event_id,
        grant_id=grant_id,
        rule_version_id=finance.version_id,
        step_index=0,
        service_team_id=finance.team_id,
    )


def test_native_workflow_silent_at_five_and_alerts_finance_at_six(
    db_session: Session, test_service, clock: Clock
) -> None:
    finance = _finance(db_session)
    handler = AutomationEventHandler()
    for count in range(1, 7):
        grant_id, event = _create(db_session, test_service, clock)
        assert Evidence.model_validate(event.payload).count_7d == count
        handler.handle(db_session, event)
        assert db_session.query(AdminNotification).count() == (0 if count <= 5 else 2)
        db_session.commit()
    assert {row.system_user_id for row in db_session.query(AdminNotification)} == set(
        finance.recipients
    )
    assert {
        row.recipient
        for row in db_session.query(Notification).filter(
            Notification.channel == NotificationChannel.email
        )
    } == set(finance.emails)
    assert (
        db_session.query(Notification).count() == 4
        and db_session.query(Review).count() == 1
    )
    assert all(
        "Test Connections created: 6" in row.body
        for row in db_session.query(AdminNotification)
    )
    assert all(
        row.target_url == f"/admin/catalog/subscriptions/{test_service.subscription_id}"
        for row in db_session.query(AdminNotification)
    )
    db_session.commit()
    handler.handle(db_session, event)
    assert db_session.query(Notification).count() == 4


def test_window_excludes_old_and_future_and_counts_ended_grants(
    db_session: Session, test_service, clock: Clock
) -> None:
    for at in (
        NOW - timedelta(days=7),
        NOW - timedelta(days=8),
        NOW + timedelta(seconds=1),
        NOW - timedelta(days=1),
        NOW - timedelta(days=2),
    ):
        db_session.add(
            Grant(
                subscription_id=test_service.subscription_id,
                subscriber_id=test_service.subscriber_id,
                actor_id=test_service.actor_id,
                actor_label="Historical staff",
                command_id=uuid4(),
                activated_at=at,
                expires_at=at + timedelta(hours=1),
                ended_at=at + timedelta(hours=1),
                duration_seconds=3600,
                delivery_state="applied",
            )
        )
    _, event = _create(db_session, test_service, clock)
    evidence = Evidence.model_validate(event.payload)
    assert evidence.count_7d == 3 and len(evidence.recent_connections) == 3
    assert evidence.window_start == NOW - timedelta(days=7)


def test_native_creation_replay_keeps_same_event_and_count(
    db_session: Session, test_service, clock: Clock
) -> None:
    key = uuid4()
    first, first_event = _create(db_session, test_service, clock, key=key)
    second, second_event = _create(db_session, test_service, clock, key=key)
    assert first == second and first_event.event_id == second_event.event_id
    assert second_event.payload["count_7d"] == 1
    assert (
        db_session.query(EventStore)
        .filter_by(event_type=EventType.test_connection_created.value)
        .count()
        == 1
    )


def test_finance_replay_freezes_recipients(
    db_session: Session, test_service, clock: Clock
) -> None:
    finance = _finance(db_session)
    grant_id, event = _create(db_session, test_service, clock)
    command = _notify(finance, grant_id, event)
    result = notify_test_connection_finance(db_session, command)
    user, person = add_bound_staff_user(
        db_session, email=f"new-finance-{uuid4()}@example.com"
    )
    db_session.add(
        ServiceTeamMember(team_id=finance.team_id, person_id=person.id, is_active=True)
    )
    db_session.commit()
    replay = notify_test_connection_finance(db_session, command)
    assert (
        replay.replayed
        and replay.recipient_ids == result.recipient_ids
        and user.id not in replay.recipient_ids
    )
    assert db_session.query(Notification).count() == 4
    db_session.commit()
    with pytest.raises(FinanceError, match="different evidence"):
        notify_test_connection_finance(
            db_session, replace(command, service_team_id=uuid4())
        )


@pytest.mark.parametrize(
    "field", ("tenant_id", "event_id", "grant_id", "service_team_id")
)
def test_finance_action_fails_closed(
    db_session: Session, test_service, clock: Clock, field: str
) -> None:
    finance = _finance(db_session)
    grant_id, event = _create(db_session, test_service, clock)
    with pytest.raises(FinanceError):
        notify_test_connection_finance(
            db_session, replace(_notify(finance, grant_id, event), **{field: uuid4()})
        )
    assert (
        db_session.query(Notification).count() == db_session.query(Review).count() == 0
    )


def test_delayed_event_uses_original_count(
    db_session: Session, test_service, clock: Clock
) -> None:
    _finance(db_session)
    events = [_create(db_session, test_service, clock)[1] for _ in range(8)]
    AutomationEventHandler().handle(db_session, events[5])
    assert db_session.query(AdminNotification).count() == 2
    assert all(
        "Test Connections created: 6" in row.body
        for row in db_session.query(AdminNotification)
    )


def test_native_references_bounded_but_count_complete(
    db_session: Session, test_service, clock: Clock
) -> None:
    events = [_create(db_session, test_service, clock)[1] for _ in range(12)]
    evidence = Evidence.model_validate(events[-1].payload)
    assert evidence.count_7d == 12 and len(evidence.recent_connections) == 10
    assert evidence.grant_id in {item.grant_id for item in evidence.recent_connections}


def test_customer_isolation_ignores_other_accounts(
    db_session: Session, test_service, clock: Clock
) -> None:
    original = db_session.get(Subscription, test_service.subscription_id)
    assert original is not None
    other = Subscriber(
        first_name="Other", last_name="Account", email=f"other-{uuid4()}@example.com"
    )
    db_session.add(other)
    db_session.flush()
    other_sub = Subscription(
        subscriber_id=other.id,
        offer_id=original.offer_id,
        status=SubscriptionStatus.suspended,
        login=f"other-{uuid4()}",
    )
    db_session.add(other_sub)
    db_session.flush()
    for _ in range(6):
        db_session.add(
            Grant(
                subscription_id=other_sub.id,
                subscriber_id=other.id,
                actor_id=test_service.actor_id,
                actor_label="Other customer staff",
                command_id=uuid4(),
                activated_at=NOW - timedelta(hours=2),
                expires_at=NOW - timedelta(hours=1),
                ended_at=NOW - timedelta(hours=1),
                duration_seconds=3600,
                delivery_state="applied",
            )
        )
    _, event = _create(db_session, test_service, clock)
    assert event.payload["count_7d"] == 1


def test_partial_finance_staging_rolls_back(
    db_session: Session, test_service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import test_connection_finance as service

    finance = _finance(db_session)
    grant_id, event = _create(db_session, test_service, clock)
    stage = service.stage_staff_direct_notification
    calls = 0

    def fail_second(db, command):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected staging failure")
        return stage(db, command)

    monkeypatch.setattr(service, "stage_staff_direct_notification", fail_second)
    with pytest.raises(RuntimeError, match="injected"):
        notify_test_connection_finance(db_session, _notify(finance, grant_id, event))
    assert (
        db_session.query(Notification).count()
        == db_session.query(AdminNotification).count()
        == db_session.query(Review).count()
        == 0
    )
