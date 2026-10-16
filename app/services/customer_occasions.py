"""Single owner of customer birthday and original-service anniversary admission."""

from calendar import isleap
from datetime import UTC, date, datetime
from hashlib import sha256
from zoneinfo import ZoneInfo

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import extract, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.customer_occasion import CustomerOccasionAutomation
from app.models.notification import (
    CommunicationIntentRecord,
    Notification,
    NotificationChannel,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.communication_intents import (
    CommunicationClass,
    CommunicationIntent,
    submit,
)
from app.services.customer_notification_policy import (
    CustomerNotificationPolicyCandidate,
    CustomerNotificationPolicyCohortQuery,
    evaluate_bulk_customer_notification_policy,
)
from app.services.customer_occasion_contracts import (
    CustomerOccasion,
    OccasionActivity,
    OccasionDeliveryDecision,
    OccasionDeliveryQuery,
    OccasionProcessResult,
    OccasionRecipient,
    OccasionReview,
    OccasionReviewQuery,
    OccasionReviewResult,
    OccasionSettings,
    OccasionTestResult,
    ProcessOccasionCommand,
    SaveOccasionCommand,
    TestOccasionCommand,
)
from app.services.customer_occasion_values import defaults as defaults
from app.services.customer_occasion_values import occasion_years as occasion_years
from app.services.customer_occasion_values import render as _render
from app.services.customer_occasion_values import validate as _validate
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.notification_status_policy import (
    ANNIVERSARY_ALLOWED_STATUSES as ANNIVERSARY_STATUSES,
)
from app.services.owner_commands import OwnerCommandDefinition, execute_owner_command

OWNER = "communications.customer_occasions"
CONCERN = "annual customer greeting configuration and admission"


def _error(suffix: str, message: str) -> DomainError:
    return DomainError(code=f"{OWNER}.{suffix}", message=message, retryable=False)


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _error("invalid_command", "An explicit timezone is required.")


def _settings(
    row: CustomerOccasionAutomation | None, occasion: CustomerOccasion
) -> OccasionSettings:
    if row is None:
        return defaults(occasion)
    return OccasionSettings.model_validate(row, from_attributes=True)


def _evidence(
    db: Session,
    *,
    occasion: CustomerOccasion,
    actor: str,
    revision: int,
    processed: int = 0,
) -> None:
    emit_event(
        db,
        EventType.customer_occasion_changed,
        {
            "schema_version": 1,
            "occasion": occasion.value,
            "revision": revision,
            "processed": processed,
        },
        actor=actor,
        record_only=True,
    )


def save(db: Session, *, command: SaveOccasionCommand) -> OccasionSettings:
    def operation() -> OccasionSettings:
        settings = command.settings
        _validate(settings)
        row = db.scalar(
            select(CustomerOccasionAutomation)
            .where(CustomerOccasionAutomation.occasion == settings.occasion.value)
            .with_for_update()
        )
        if row is None:
            raise _error(
                "not_configured", "Apply the customer occasion migration first."
            )
        if row.revision != settings.revision:
            raise _error("stale_revision", "Settings changed. Reload before saving.")
        row.enabled = settings.enabled
        row.subject = settings.subject
        row.body = settings.body
        row.timezone = settings.timezone
        row.send_hour = settings.send_hour
        row.send_minute = settings.send_minute
        row.revision += 1
        row.updated_at = datetime.now(UTC)
        db.flush()
        _evidence(
            db,
            occasion=settings.occasion,
            actor=command.context.actor,
            revision=row.revision,
        )
        return _settings(row, settings.occasion)

    return execute_owner_command(
        db,
        definition=OwnerCommandDefinition(owner=OWNER, concern=CONCERN, name="save"),
        context=command.context,
        operation=operation,
    )


def send_test(db: Session, *, command: TestOccasionCommand) -> OccasionTestResult:
    """An explicit operator test is independent of customer automation admission."""

    def operation() -> OccasionTestResult:
        try:
            recipient = str(TypeAdapter(EmailStr).validate_python(command.recipient))
        except ValidationError as exc:
            raise _error(
                "invalid_command", "Enter a valid test email address."
            ) from exc
        row = db.scalar(
            select(CustomerOccasionAutomation)
            .where(CustomerOccasionAutomation.occasion == command.occasion.value)
            .with_for_update()
        )
        if row is None:
            raise _error(
                "not_configured", "Apply the customer occasion migration first."
            )
        settings = _settings(row, command.occasion)
        key = f"occasion-test:{command.occasion.value}:{command.context.command_id}"
        fingerprint = sha256(recipient.strip().lower().encode()).hexdigest()
        existing = db.scalar(
            select(CommunicationIntentRecord).where(
                CommunicationIntentRecord.dedupe_key == key
            )
        )
        if (
            existing is not None
            and (existing.metadata_ or {}).get("test_recipient_fingerprint")
            != fingerprint
        ):
            raise _error(
                "invalid_command",
                "This test reference belongs to a different recipient. Reload before testing.",
            )
        subject, body = _render(settings, first_name="Alex", years=3)
        result = submit(
            db=db,
            intent=CommunicationIntent(
                subscriber_id=None,
                event_type="customer_occasion.test",
                category="general",
                subject=f"[TEST] {subject}"[:200],
                body=body,
                communication_class=CommunicationClass.operational,
                channels=(NotificationChannel.email,),
                include_reseller=False,
                recipients={NotificationChannel.email: recipient},
                resolve_subscriber_identity=False,
                audience_type="operational",
                audience_id=command.context.command_id,
                metadata={"test_recipient_fingerprint": fingerprint},
                dedupe_key=key,
            ),
        )
        return OccasionTestResult(result.intent_id, bool(result.queued))

    return execute_owner_command(
        db,
        definition=OwnerCommandDefinition(
            owner=OWNER, concern=CONCERN, name="send_test"
        ),
        context=command.context,
        operation=operation,
    )


def _category(occasion: CustomerOccasion) -> str:
    return occasion.value


def _date_column(
    occasion: CustomerOccasion,
) -> InstrumentedAttribute[date | None] | InstrumentedAttribute[datetime | None]:
    return (
        Subscriber.date_of_birth
        if occasion == CustomerOccasion.birthday
        else Subscriber.account_start_date
    )


def _scope(occasion: CustomerOccasion) -> ColumnElement[bool]:
    if occasion == CustomerOccasion.anniversary:
        return Subscriber.status.in_(ANNIVERSARY_STATUSES)
    return Subscriber.status.notin_(
        (SubscriberStatus.disabled, SubscriberStatus.canceled)
    )


def _matches(occasion: CustomerOccasion, today: date) -> ColumnElement[bool]:
    column = _date_column(occasion)
    day = extract("day", column) == today.day
    if today.month == 2 and today.day == 28 and not isleap(today.year):
        day = or_(day, extract("day", column) == 29)
    year = (
        extract("year", column) < today.year
        if occasion == CustomerOccasion.anniversary
        else extract("year", column) <= today.year
    )
    return (extract("month", column) == today.month) & day & year


def _key(occasion: CustomerOccasion, subscriber: Subscriber, year: int) -> str:
    return f"customer-occasion:{occasion.value}:{subscriber.id}:{year}"


def _reason(
    db: Session, *, subscriber: Subscriber, occasion: CustomerOccasion, now: datetime
) -> str | None:
    if not subscriber.email or not subscriber.email.strip():
        return "missing_email"
    if not subscriber.marketing_opt_in:
        return "marketing_opt_out"
    result = evaluate_bulk_customer_notification_policy(
        db,
        query=CustomerNotificationPolicyCohortQuery(
            candidates=(
                CustomerNotificationPolicyCandidate(
                    subscriber_id=subscriber.id, recipient=subscriber.email
                ),
            ),
            channel=NotificationChannel.email,
            category=_category(occasion),
            event_type=f"customer.{occasion.value}",
            evaluated_at=now,
        ),
    )
    decision = result.decisions[0]
    return decision.reason_code if not decision.allowed else None


def process(db: Session, *, command: ProcessOccasionCommand) -> OccasionProcessResult:
    def operation() -> OccasionProcessResult:
        _aware(command.now)
        if not 1 <= command.limit <= 1000:
            raise _error("invalid_command", "Batch size must be between 1 and 1000.")
        occasion = command.occasion
        row = db.scalar(
            select(CustomerOccasionAutomation)
            .where(CustomerOccasionAutomation.occasion == occasion.value)
            .with_for_update(skip_locked=True)
        )
        if row is None or not row.enabled:
            return OccasionProcessResult(occasion, reason="disabled_or_locked")
        settings = _settings(row, occasion)
        _validate(settings)
        local = command.now.astimezone(ZoneInfo(settings.timezone))
        if (local.hour, local.minute) < (settings.send_hour, settings.send_minute):
            return OccasionProcessResult(occasion, reason="before_send_time")
        # Config-row locking serializes this occasion; durable intent uniqueness
        # arbitrates annual identity. No year/date is accepted from the task.
        seen = (
            select(CommunicationIntentRecord.id)
            .where(
                CommunicationIntentRecord.subscriber_id == Subscriber.id,
                CommunicationIntentRecord.event_type == f"customer.{occasion.value}",
                CommunicationIntentRecord.metadata_["occasion_year"].as_integer()
                == local.year,
            )
            .exists()
        )
        subscribers = db.scalars(
            select(Subscriber)
            .where(
                _scope(occasion),
                _matches(occasion, local.date()),
                ~seen,
                Subscriber.email.is_not(None),
                func.trim(Subscriber.email) != "",
            )
            .order_by(Subscriber.id)
            .limit(command.limit)
            .with_for_update()
        ).all()
        queued = suppressed = 0
        for subscriber in subscribers:
            original = (
                subscriber.date_of_birth
                if occasion == CustomerOccasion.birthday
                else (
                    subscriber.account_start_date.date()
                    if subscriber.account_start_date
                    else None
                )
            )
            if original is None:
                continue
            years = occasion_years(original=original, today=local.date())
            if years is None or (
                occasion == CustomerOccasion.anniversary and years < 1
            ):
                continue
            subject, body = _render(
                settings, first_name=subscriber.first_name or "there", years=years
            )
            result = submit(
                db=db,
                intent=CommunicationIntent(
                    subscriber_id=subscriber.id,
                    event_type=f"customer.{occasion.value}",
                    category=_category(occasion),
                    subject=subject,
                    body=body,
                    communication_class=CommunicationClass.marketing,
                    channels=(NotificationChannel.email,),
                    include_reseller=False,
                    recipients={NotificationChannel.email: subscriber.email}
                    if subscriber.email
                    else {},
                    metadata={
                        "occasion": occasion.value,
                        "occasion_year": local.year,
                        "configuration_revision": row.revision,
                    },
                    dedupe_key=_key(occasion, subscriber, local.year),
                ),
            )
            queued += len(result.queued)
            suppressed += int(not result.queued)
        if subscribers:
            _evidence(
                db,
                occasion=occasion,
                actor=command.context.actor,
                revision=row.revision,
                processed=len(subscribers),
            )
        return OccasionProcessResult(
            occasion, processed=len(subscribers), queued=queued, suppressed=suppressed
        )

    return execute_owner_command(
        db,
        definition=OwnerCommandDefinition(owner=OWNER, concern=CONCERN, name="process"),
        context=command.context,
        operation=operation,
    )


def review(db: Session, *, query: OccasionReviewQuery) -> OccasionReviewResult:
    _aware(query.now)
    if not 1 <= query.sample_limit <= 50:
        raise _error("invalid_command", "Preview size must be between 1 and 50.")
    reviews: list[OccasionReview] = []
    for occasion in CustomerOccasion:
        settings = _settings(
            db.get(CustomerOccasionAutomation, occasion.value), occasion
        )
        today = query.now.astimezone(ZoneInfo(settings.timezone)).date()
        predicates = (_scope(occasion), _matches(occasion, today))
        matched = (
            db.scalar(select(func.count()).select_from(Subscriber).where(*predicates))
            or 0
        )
        missing = (
            db.scalar(
                select(func.count())
                .select_from(Subscriber)
                .where(_scope(occasion), _date_column(occasion).is_(None))
            )
            or 0
        )
        sample = db.scalars(
            select(Subscriber)
            .where(*predicates)
            .order_by(Subscriber.id)
            .limit(query.sample_limit)
        ).all()
        recipients: list[OccasionRecipient] = []
        for subscriber in sample:
            original = (
                subscriber.date_of_birth
                if occasion == CustomerOccasion.birthday
                else (
                    subscriber.account_start_date.date()
                    if subscriber.account_start_date
                    else None
                )
            )
            years = occasion_years(original=original, today=today) if original else None
            recipients.append(
                OccasionRecipient(
                    subscriber_id=subscriber.id,
                    name=subscriber.first_name or "Customer",
                    email=subscriber.email or "",
                    years=years or 0,
                    reason=_reason(
                        db, subscriber=subscriber, occasion=occasion, now=query.now
                    ),
                )
            )
        subject, body = _render(settings, first_name="Alex", years=3)
        reviews.append(
            OccasionReview(
                settings, today, matched, missing, tuple(recipients), subject, body
            )
        )
    activity = db.scalars(
        select(CommunicationIntentRecord)
        .where(
            CommunicationIntentRecord.event_type.in_(
                ("customer.birthday", "customer.anniversary")
            )
        )
        .order_by(CommunicationIntentRecord.created_at.desc())
        .limit(20)
    ).all()
    return OccasionReviewResult(
        query.now,
        tuple(reviews),
        tuple(
            OccasionActivity(
                CustomerOccasion(row.event_type.removeprefix("customer.")),
                row.created_at,
                row.status,
                tuple(row.suppression_reasons or ()),
            )
            for row in activity
        ),
    )


def delivery_decision(
    db: Session, *, query: OccasionDeliveryQuery
) -> OccasionDeliveryDecision:
    """Read-only last-mile policy; never changes a queued message or transaction."""
    _aware(query.now)
    notification = db.get(Notification, query.notification_id)
    if notification is None:
        return OccasionDeliveryDecision(False, "notification_missing")
    if notification.event_type not in {"customer.birthday", "customer.anniversary"}:
        return OccasionDeliveryDecision(True)
    subscriber = (
        db.get(Subscriber, notification.subscriber_id)
        if notification.subscriber_id
        else None
    )
    if subscriber is None:
        return OccasionDeliveryDecision(False, "customer_missing")
    occasion = CustomerOccasion(notification.event_type.removeprefix("customer."))
    from app.services.customer_notification_policy import (
        is_notification_enabled_for_subscriber,
    )
    from app.services.notification_status_policy import status_allows_notification

    if not status_allows_notification(subscriber.status, _category(occasion)):
        return OccasionDeliveryDecision(False, "account_status")
    if not subscriber.marketing_opt_in:
        return OccasionDeliveryDecision(False, "marketing_opt_out")
    if (
        not subscriber.email
        or notification.recipient.strip().lower() != subscriber.email.strip().lower()
    ):
        return OccasionDeliveryDecision(False, "email_changed")
    if not is_notification_enabled_for_subscriber(
        db=db,
        subscriber_id=subscriber.id,
        channel=notification.channel,
        category=_category(occasion),
        recipient=notification.recipient,
    ):
        return OccasionDeliveryDecision(False, "preferences")
    return OccasionDeliveryDecision(True)
