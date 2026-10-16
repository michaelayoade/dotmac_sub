"""Fast unit evidence only; SQLite is not deployed-schema acceptance."""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import EmailStr, TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer_occasion import CustomerOccasionAutomation
from app.models.notification import CommunicationIntentRecord, Notification
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services import customer_occasions as owner
from app.services.communication_eligibility import is_marketing
from app.services.customer_occasion_contracts import (
    CustomerOccasion,
    OccasionDeliveryQuery,
    OccasionProcessResult,
    OccasionReviewQuery,
    ProcessOccasionCommand,
    SaveOccasionCommand,
)
from app.services.customer_occasion_contracts import (
    TestOccasionCommand as SendTestCommand,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.notification_status_policy import status_allows_notification
from app.services.owner_commands import CommandContext

NOW = datetime(2026, 10, 16, 10, tzinfo=UTC)


def context() -> CommandContext:
    return CommandContext.system(
        actor="test:occasions",
        scope="test:occasions",
        reason="Verify annual greeting rules",
    )


def configure(
    db: Session, occasion: CustomerOccasion, *, enabled: bool = False
) -> None:
    settings = owner.defaults(occasion)
    db.add(
        CustomerOccasionAutomation(
            **settings.model_copy(update={"enabled": enabled}).model_dump()
        )
    )
    db.flush()


def customer(
    db: Session,
    *,
    status: SubscriberStatus = SubscriberStatus.active,
    birth: date | None = date(1990, 10, 16),
    start: datetime | None = datetime(2023, 10, 16, tzinfo=UTC),
    consent: bool = True,
) -> Subscriber:
    row = Subscriber(
        id=uuid4(),
        first_name="Alex",
        last_name="Example",
        email=f"{uuid4()}@example.com",
        status=status,
        date_of_birth=birth,
        account_start_date=start,
        marketing_opt_in=consent,
    )
    db.add(row)
    db.flush()
    return row


def process(
    db: Session, occasion: CustomerOccasion, *, now: datetime = NOW, limit: int = 200
) -> OccasionProcessResult:
    db_session_adapter.release_read_transaction(db)
    return owner.process(
        db=db,
        command=ProcessOccasionCommand(
            context=context(), occasion=occasion, now=now, limit=limit
        ),
    )


@pytest.mark.parametrize("occasion", list(CustomerOccasion))
def test_disabled_by_default_does_not_create_intents(
    db_session: Session, occasion: CustomerOccasion
) -> None:
    configure(db_session, occasion)
    customer(db_session)
    result = process(db_session, occasion)
    assert result.processed == 0
    assert db_session.scalar(select(CommunicationIntentRecord.id)) is None


@pytest.mark.parametrize("status", list(SubscriberStatus))
def test_anniversary_status_gate_is_exact(status: SubscriberStatus) -> None:
    assert status_allows_notification(status, "anniversary") == (
        status in owner.ANNIVERSARY_STATUSES
    )
    assert is_marketing("anniversary") and is_marketing("birthday")
    assert not status_allows_notification(None, "anniversary")


@pytest.mark.parametrize("status", list(SubscriberStatus))
def test_anniversary_only_materializes_active_and_suspended(
    db_session: Session, status: SubscriberStatus
) -> None:
    configure(db_session, CustomerOccasion.anniversary, enabled=True)
    customer(db_session, status=status)
    result = process(db_session, CustomerOccasion.anniversary)
    assert result.processed == int(status in owner.ANNIVERSARY_STATUSES)
    assert result.queued == int(status in owner.ANNIVERSARY_STATUSES)


def test_annual_replay_and_bounded_batches(db_session: Session) -> None:
    configure(db_session, CustomerOccasion.birthday, enabled=True)
    customer(db_session)
    customer(db_session)
    assert process(db_session, CustomerOccasion.birthday, limit=1).processed == 1
    assert process(db_session, CustomerOccasion.birthday, limit=1).processed == 1
    assert process(db_session, CustomerOccasion.birthday, limit=1).processed == 0
    assert len(db_session.scalars(select(CommunicationIntentRecord)).all()) == 2


def test_missing_start_has_no_record_creation_fallback_and_first_year_is_excluded(
    db_session: Session,
) -> None:
    configure(db_session, CustomerOccasion.anniversary, enabled=True)
    customer(db_session, start=None)
    customer(db_session, start=NOW)
    customer(db_session, start=datetime(2027, 10, 16, tzinfo=UTC))
    assert process(db_session, CustomerOccasion.anniversary).processed == 0


def test_no_consent_records_suppression_once(db_session: Session) -> None:
    configure(db_session, CustomerOccasion.birthday, enabled=True)
    customer(db_session, consent=False)
    result = process(db_session, CustomerOccasion.birthday)
    assert result.queued == 0 and result.suppressed == 1
    assert process(db_session, CustomerOccasion.birthday).processed == 0


def test_timezone_and_send_time(db_session: Session) -> None:
    configure(db_session, CustomerOccasion.birthday, enabled=True)
    customer(db_session)
    assert (
        process(
            db_session,
            CustomerOccasion.birthday,
            now=datetime(2026, 10, 16, 7, 59, tzinfo=UTC),
        ).processed
        == 0
    )
    assert (
        process(
            db_session,
            CustomerOccasion.birthday,
            now=datetime(2026, 10, 16, 8, tzinfo=UTC),
        ).processed
        == 1
    )


@pytest.mark.parametrize(
    ("original", "today", "expected"),
    [
        (date(2020, 2, 29), date(2026, 2, 28), 6),
        (date(2020, 2, 29), date(2024, 2, 28), None),
        (date(2020, 2, 29), date(2024, 2, 29), 4),
        (date(2027, 10, 16), date(2026, 10, 16), None),
    ],
)
def test_calendar_rules(original: date, today: date, expected: int | None) -> None:
    assert owner.occasion_years(original=original, today=today) == expected


def test_preview_is_read_only_and_reports_missing_dates(db_session: Session) -> None:
    configure(db_session, CustomerOccasion.birthday)
    configure(db_session, CustomerOccasion.anniversary)
    customer(db_session, start=None)
    review = owner.review(db=db_session, query=OccasionReviewQuery(now=NOW))
    assert review.automations[1].missing_date_count == 1
    assert all(not item.settings.enabled for item in review.automations)
    assert db_session.scalar(select(CommunicationIntentRecord.id)) is None


def test_save_stale_revision_and_invalid_template_fail_closed(
    db_session: Session,
) -> None:
    configure(db_session, CustomerOccasion.birthday)
    initial = owner.defaults(CustomerOccasion.birthday)
    db_session_adapter.release_read_transaction(db_session)
    result = owner.save(db=db_session, command=SaveOccasionCommand(context(), initial))
    assert result.revision == 2 and not result.enabled
    with pytest.raises(DomainError, match="Reload"):
        owner.save(db=db_session, command=SaveOccasionCommand(context(), initial))
    invalid = result.model_copy(update={"body": "{first_name.__class__}"})
    with pytest.raises(DomainError, match="Invalid"):
        owner.save(db=db_session, command=SaveOccasionCommand(context(), invalid))


def test_delivery_rechecks_status_consent_and_preferences(db_session: Session) -> None:
    configure(db_session, CustomerOccasion.anniversary, enabled=True)
    subscriber = customer(db_session, status=SubscriberStatus.suspended)
    assert process(db_session, CustomerOccasion.anniversary).queued == 1
    notification = db_session.scalar(
        select(Notification).where(Notification.event_type == "customer.anniversary")
    )
    assert notification is not None
    query = OccasionDeliveryQuery(notification.id, NOW)
    assert owner.delivery_decision(db=db_session, query=query).allowed
    subscriber.status = SubscriberStatus.blocked
    db_session.flush()
    assert not owner.delivery_decision(db=db_session, query=query).allowed
    subscriber.status = SubscriberStatus.active
    subscriber.marketing_opt_in = False
    assert not owner.delivery_decision(db=db_session, query=query).allowed


def test_plain_text_templates_escape_customer_content() -> None:
    subject, body = owner._render(
        owner.defaults(CustomerOccasion.birthday), first_name="<script>\r\nX", years=3
    )
    assert "<script>" not in body and "&lt;script&gt;" in body
    assert "\r" not in subject and "\n" not in subject


def test_explicit_test_does_not_enable_automation_or_consume_annual_send(
    db_session: Session,
) -> None:
    configure(db_session, CustomerOccasion.anniversary)
    command = SendTestCommand(
        context(),
        CustomerOccasion.anniversary,
        TypeAdapter(EmailStr).validate_python("reviewer@example.com"),
    )
    db_session_adapter.release_read_transaction(db_session)
    first = owner.send_test(db=db_session, command=command)
    second = owner.send_test(db=db_session, command=command)
    assert first.intent_id == second.intent_id and first.queued
    assert not db_session.get(CustomerOccasionAutomation, "anniversary").enabled
    rows = db_session.scalars(select(CommunicationIntentRecord)).all()
    assert len(rows) == 1 and rows[0].event_type == "customer_occasion.test"
