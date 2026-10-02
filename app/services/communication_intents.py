"""Source of truth for customer and reseller communication decisions."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.models.notification import (
    CommunicationIntentRecipient,
    CommunicationIntentRecord,
    Notification,
    NotificationChannel,
    NotificationIntentCoverage,
    NotificationStatus,
)
from app.models.subscriber import Reseller, ResellerUser, Subscriber, SubscriberContact
from app.models.system_user import SystemUser
from app.schemas.notification import NotificationCreate, NotificationDeliveryLatency
from app.services.communication_eligibility import suppression_reason
from app.services.customer_notification_policy import (
    resolve_subscriber_id_for_recipient,
)
from app.services.domain_errors import DomainError
from app.services.events.dispatcher import emit_event
from app.services.events.types import EventType
from app.services.notification_channel_policy import resolve_notification_channels
from app.services.operator_tenant import operator_tenant_id

MAX_EMAIL_ATTACHMENT_BYTES = 10 * 1024 * 1024


class CommunicationClass(enum.StrEnum):
    transactional = "transactional"
    marketing = "marketing"
    operational = "operational"


class CommunicationAttachmentKind(enum.StrEnum):
    invoice_pdf = "invoice_pdf"
    quote_pdf = "quote_pdf"
    ncc_weekly_csv = "ncc_weekly_csv"
    ncc_weekly_xlsx = "ncc_weekly_xlsx"


@dataclass(frozen=True)
class CommunicationAttachment:
    """Durable reference to content materialized only during delivery."""

    kind: CommunicationAttachmentKind
    entity_id: UUID
    filename: str
    content_type: str = "application/pdf"

    def to_metadata(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "entity_id": str(self.entity_id),
            "filename": self.filename,
            "content_type": self.content_type,
        }


@dataclass(frozen=True)
class CommunicationIntent:
    subscriber_id: UUID | None
    event_type: str
    category: str
    subject: str | None
    body: str | None
    template_id: UUID | None = None
    template_code: str | None = None
    communication_class: CommunicationClass = CommunicationClass.transactional
    default_channels: tuple[NotificationChannel, ...] = (NotificationChannel.email,)
    channels: tuple[NotificationChannel, ...] | None = None
    include_reseller: bool = True
    persist_policy_suppressions: bool = True
    recipients: dict[NotificationChannel, str] = field(default_factory=dict)
    audience_type: str = "subscriber"
    audience_id: UUID | None = None
    resolve_subscriber_identity: bool = True
    metadata: dict[str, object] = field(default_factory=dict)
    attachments: tuple[CommunicationAttachment, ...] = ()
    dedupe_key: str | None = None
    send_at: datetime | None = None
    requested_status: NotificationStatus = NotificationStatus.queued
    requested_last_error: str | None = None
    delivery_latency: NotificationDeliveryLatency = NotificationDeliveryLatency.normal


@dataclass(frozen=True)
class CommunicationIntentResult:
    intent_id: UUID
    deliveries: tuple[Notification, ...]
    queued: tuple[Notification, ...]
    suppressed: tuple[str, ...]
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class PlannedRecipient:
    decision_id: UUID
    intent_id: UUID
    planned_at: datetime
    subscriber_id: UUID | None
    audience_type: str
    audience_id: UUID | None
    channel: NotificationChannel
    recipient: str | None
    normalized_recipient: str | None
    accepted: bool
    suppression_reason: str | None
    event_type: str
    category: str
    communication_class: CommunicationClass
    subject: str | None
    body: str | None
    attachments: tuple[CommunicationAttachment, ...]
    canonical_send_at: datetime | None
    has_attachment_metadata: bool = False


@dataclass(frozen=True, slots=True)
class CommunicationIntentPlan:
    intent_id: UUID
    recipients: tuple[PlannedRecipient, ...]
    suppressed: tuple[str, ...]
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class PlannedRecipientExecution:
    decision_id: UUID
    notification_id: UUID | None
    notification: Notification | None
    queued: bool
    suppression_reason: str | None


@dataclass(frozen=True, slots=True)
class CoveredNotificationRecheck:
    notification_id: UUID
    eligible_decision_ids: tuple[UUID, ...]
    suppressed_decision_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class CommunicationIntentPlannedEvidence:
    schema_version: int
    intent_id: str
    accepted_count: int
    suppressed_count: int

    def payload(self) -> dict[str, str | int]:
        return {
            "schema_version": self.schema_version,
            "intent_id": self.intent_id,
            "accepted_count": self.accepted_count,
            "suppressed_count": self.suppressed_count,
        }


def _intent_error(code: str, message: str) -> DomainError:
    return DomainError(
        code=f"communications.intents.{code}", message=message, retryable=False
    )


def list_intents(
    db: Session,
    *,
    subscriber_id: UUID | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[CommunicationIntentRecord]:
    query = db.query(CommunicationIntentRecord)
    if subscriber_id is not None:
        query = query.filter(CommunicationIntentRecord.subscriber_id == subscriber_id)
    if status:
        query = query.filter(CommunicationIntentRecord.status == status)
    return list(
        query.order_by(CommunicationIntentRecord.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def _subscriber_addresses(
    db: Session,
    subscriber: Subscriber,
    channel: NotificationChannel,
    category: str | None = None,
) -> list[str]:
    """Compose the addresses a customer communication should reach.

    `SubscriberContact` carries a `contact_type` and an `is_billing_contact`
    flag, and until now nothing read either: every contact with
    `receives_notifications` received everything. A contact typed *technical*
    got billing notices, and naming a billing contact selected nobody.

    Roles are honoured only when the account has actually designated one. If a
    subscriber has a billing contact, billing communications go to them; if
    they have not, every notification contact keeps receiving billing as
    before. That way switching this on takes nothing away from an account that
    never expressed a preference — the model starts meaning something without
    silently narrowing anyone's mail.

    The account holder's own address is always included regardless.
    """
    addresses: list[str] = []
    if channel == NotificationChannel.email:
        if subscriber.email:
            addresses.append(subscriber.email)
    elif channel in {NotificationChannel.sms, NotificationChannel.whatsapp}:
        if subscriber.phone:
            addresses.append(subscriber.phone)
    elif channel == NotificationChannel.push:
        addresses.append(str(subscriber.id))

    if channel != NotificationChannel.push:
        contacts = (
            db.query(SubscriberContact)
            .filter(SubscriberContact.subscriber_id == subscriber.id)
            .filter(SubscriberContact.receives_notifications.is_(True))
            .all()
        )
        if str(category or "").strip().lower() == "billing":
            designated = [contact for contact in contacts if contact.is_billing_contact]
            # Only narrow when the account has actually named someone.
            if designated:
                contacts = designated
        for contact in contacts:
            if channel == NotificationChannel.email and contact.email:
                addresses.append(contact.email)
            elif channel == NotificationChannel.sms and contact.phone:
                addresses.append(contact.phone)
            elif channel == NotificationChannel.whatsapp:
                address = contact.whatsapp or contact.phone
                if address:
                    addresses.append(address)
    return list(dict.fromkeys(address.strip() for address in addresses if address))


def _reseller_addresses(
    db: Session, reseller: Reseller, channel: NotificationChannel
) -> list[str]:
    addresses: list[str] = []
    if channel == NotificationChannel.email and reseller.contact_email:
        addresses.append(reseller.contact_email)
    elif channel in {NotificationChannel.sms, NotificationChannel.whatsapp}:
        if reseller.contact_phone:
            addresses.append(reseller.contact_phone)
    if channel == NotificationChannel.email:
        # Guard against main-reseller-customer-mail-copy-leak: a ResellerUser
        # row is customer-notification audience only when the identity behind
        # it is actually a reseller. A row whose linked identity is ALSO a
        # registered, active SystemUser (a platform administrator) is never
        # eligible here, regardless of which reseller it is nominally
        # attached to or whether that reseller is flagged `is_house` -- an
        # admin account misfiled as a reseller-portal login must not become
        # a silent copy target for every eligible customer notification.
        # "Linked identity" is checked two ways because production data is
        # inconsistent about which one is populated: a shared bound
        # `person_party_id` (the canonical identity link), and a
        # case-insensitive email match (the shape the historical incident
        # row actually had -- no party binding, same address on both rows).
        is_system_user_identity = (
            db.query(SystemUser.id)
            .filter(
                SystemUser.is_active.is_(True),
                or_(
                    and_(
                        ResellerUser.person_party_id.is_not(None),
                        SystemUser.person_party_id == ResellerUser.person_party_id,
                    ),
                    func.lower(SystemUser.email) == func.lower(ResellerUser.email),
                ),
            )
            .exists()
        )
        addresses.extend(
            email
            for (email,) in db.query(ResellerUser.email)
            .filter(ResellerUser.reseller_id == reseller.id)
            .filter(ResellerUser.is_active.is_(True))
            .filter(ResellerUser.email.is_not(None))
            .filter(~is_system_user_identity)
            .all()
            if email
        )
    return list(dict.fromkeys(address.strip() for address in addresses if address))


def _normalized_recipient(
    channel: NotificationChannel, recipient: str | None
) -> str | None:
    if recipient is None:
        return None
    value = recipient.strip()
    return value.casefold() if channel == NotificationChannel.email else value


def _attachment_refs(
    record: CommunicationIntentRecord,
) -> tuple[CommunicationAttachment, ...]:
    metadata = record.metadata_ or {}
    raw = metadata.get("attachments", [])
    if not isinstance(raw, list):
        return ()
    attachments: list[CommunicationAttachment] = []
    for item in raw:
        # Inbox and other incumbent transports also store opaque envelopes
        # here. Decode only our typed domain references; preserve the original
        # metadata for the transport that owns the remaining shapes.
        if not isinstance(item, dict) or item.get("kind") not in {
            kind.value for kind in CommunicationAttachmentKind
        }:
            continue
        attachments.append(
            CommunicationAttachment(
                kind=CommunicationAttachmentKind(str(item["kind"])),
                entity_id=UUID(str(item["entity_id"])),
                filename=str(item["filename"]),
                content_type=str(item.get("content_type", "application/pdf")),
            )
        )
    return tuple(attachments)


def load_planned_recipient(db: Session, decision_id: UUID) -> PlannedRecipient:
    """Read the immutable content and audience contract for one decision."""
    decision = db.get(CommunicationIntentRecipient, decision_id)
    if decision is None or decision.tenant_id != operator_tenant_id():
        raise _intent_error(
            "decision_not_found", "Communication recipient decision not found"
        )
    record = db.get(CommunicationIntentRecord, decision.intent_id)
    if record is None:
        raise _intent_error("intent_not_found", "Communication intent not found")
    return PlannedRecipient(
        decision_id=decision.id,
        intent_id=record.id,
        planned_at=(
            decision.created_at
            if decision.created_at.tzinfo
            else decision.created_at.replace(tzinfo=UTC)
        ),
        subscriber_id=decision.subscriber_id,
        audience_type=decision.audience_type,
        audience_id=decision.audience_id,
        channel=decision.channel,
        recipient=decision.recipient,
        normalized_recipient=decision.normalized_recipient,
        accepted=decision.decision == "accepted",
        suppression_reason=decision.suppression_reason,
        event_type=record.event_type,
        category=record.category,
        communication_class=CommunicationClass(record.communication_class),
        subject=record.subject,
        body=record.body,
        attachments=_attachment_refs(record),
        has_attachment_metadata=bool((record.metadata_ or {}).get("attachments")),
        canonical_send_at=(
            decision.canonical_send_at
            if decision.canonical_send_at is None or decision.canonical_send_at.tzinfo
            else decision.canonical_send_at.replace(tzinfo=UTC)
        ),
    )


def _plan_from_record(
    db: Session, record: CommunicationIntentRecord, *, replayed: bool = False
) -> CommunicationIntentPlan:
    decisions = (
        db.query(CommunicationIntentRecipient.id)
        .filter(CommunicationIntentRecipient.intent_id == record.id)
        .order_by(
            CommunicationIntentRecipient.created_at, CommunicationIntentRecipient.id
        )
        .all()
    )
    return CommunicationIntentPlan(
        intent_id=record.id,
        recipients=tuple(load_planned_recipient(db, row.id) for row in decisions),
        suppressed=tuple(record.suppression_reasons or []),
        replayed=replayed,
    )


def _stage_plan_evidence(db: Session, record: CommunicationIntentRecord) -> None:
    decisions = record.recipient_decisions
    evidence = CommunicationIntentPlannedEvidence(
        schema_version=1,
        intent_id=str(record.id),
        accepted_count=sum(row.decision == "accepted" for row in decisions),
        suppressed_count=sum(row.decision == "suppressed" for row in decisions),
    )
    emit_event(
        db,
        EventType.communication_intent_planned,
        evidence.payload(),
        subscriber_id=record.subscriber_id,
        dispatch_after_commit=False,
        record_only=True,
    )


def plan_intent(db: Session, intent: CommunicationIntent) -> CommunicationIntentPlan:
    """Persist recipient decisions and content without queuing a delivery."""
    from app.services.notification import (
        evaluate_customer_notification_policy,
        resolve_notification_timing,
    )

    if intent.audience_type not in {
        "subscriber",
        "system_user",
        "reseller_user",
        "operational",
    }:
        raise ValueError("Unsupported communication audience type")
    if intent.audience_type != "subscriber" and (
        intent.subscriber_id is not None
        or intent.include_reseller
        or intent.communication_class == CommunicationClass.marketing
    ):
        raise ValueError("Internal communication cannot use customer policies")
    if intent.audience_type != "subscriber" and intent.audience_id is None:
        raise ValueError("Internal communication requires an audience identifier")
    if intent.attachments and NotificationChannel.email not in (
        intent.channels or intent.default_channels
    ):
        raise ValueError("Communication attachments require an email channel")

    resolved_subscriber_id = intent.subscriber_id
    if (
        intent.audience_type == "subscriber"
        and intent.resolve_subscriber_identity
        and resolved_subscriber_id is None
    ):
        for identity_recipient in intent.recipients.values():
            resolved_subscriber_id = resolve_subscriber_id_for_recipient(
                db, identity_recipient
            )
            if resolved_subscriber_id is not None:
                break
    subscriber = (
        db.get(Subscriber, resolved_subscriber_id) if resolved_subscriber_id else None
    )
    if (
        intent.audience_type == "subscriber"
        and intent.subscriber_id is not None
        and subscriber is None
    ):
        raise ValueError("Subscriber not found")
    channels = intent.channels or resolve_notification_channels(
        db,
        template_code=intent.template_code,
        event_type=intent.event_type,
        category=intent.category,
        default_channels=intent.default_channels,
    )
    if intent.dedupe_key:
        existing = (
            db.query(CommunicationIntentRecord)
            .filter(CommunicationIntentRecord.dedupe_key == intent.dedupe_key)
            .one_or_none()
        )
        if existing is not None:
            return _plan_from_record(db, existing, replayed=True)

    attachment_metadata = [item.to_metadata() for item in intent.attachments]
    record = CommunicationIntentRecord(
        subscriber_id=subscriber.id if subscriber else None,
        event_type=intent.event_type,
        category=intent.category,
        communication_class=intent.communication_class.value,
        template_id=intent.template_id,
        template_code=intent.template_code,
        subject=intent.subject,
        body=intent.body,
        channels=[channel.value for channel in channels],
        include_reseller=intent.include_reseller,
        status="pending",
        suppression_reasons=[],
        dedupe_key=intent.dedupe_key,
        scheduled_for=intent.send_at,
        metadata_={
            **intent.metadata,
            **({"attachments": attachment_metadata} if attachment_metadata else {}),
            "audience_type": intent.audience_type,
            "audience_id": str(intent.audience_id) if intent.audience_id else None,
            "delivery_latency": intent.delivery_latency.value,
        },
    )
    db.add(record)
    db.flush()
    suppressed: list[str] = []

    def add_decision(
        *,
        channel: NotificationChannel,
        recipient: str | None,
        audience_type: str,
        audience_id: UUID | None,
        subscriber_id: UUID | None,
        reason: str | None,
        persist_suppression: bool,
        metadata: dict[str, object],
    ) -> None:
        timing = resolve_notification_timing(
            db,
            delivery_latency=intent.delivery_latency,
            requested_send_at=intent.send_at,
            quiet_hours_applicable=audience_type == "subscriber"
            and subscriber_id is not None,
        )
        db.add(
            CommunicationIntentRecipient(
                tenant_id=operator_tenant_id(),
                intent_id=record.id,
                audience_type=audience_type,
                audience_id=audience_id,
                subscriber_id=subscriber_id,
                channel=channel,
                recipient=recipient,
                normalized_recipient=_normalized_recipient(channel, recipient),
                decision="suppressed" if reason else "accepted",
                suppression_reason=reason,
                requested_status=intent.requested_status,
                requested_last_error=intent.requested_last_error,
                delivery_latency=intent.delivery_latency.value,
                send_at=intent.send_at,
                canonical_send_at=timing.send_at,
                persist_suppression=persist_suppression,
                metadata_=metadata,
            )
        )
        if reason:
            short_reason = reason.removeprefix("Suppressed by communication ledger: ")
            suppressed.append(f"{audience_type}:{channel.value}:{short_reason}")

    marketing_opt_out = intent.communication_class == CommunicationClass.marketing and (
        subscriber is None or not subscriber.marketing_opt_in
    )
    if marketing_opt_out:
        record.status = "suppressed"
        record.suppression_reasons = ["marketing_opt_out"]
        record.processed_at = datetime.now(UTC)
        db.flush()
        _stage_plan_evidence(db, record)
        return _plan_from_record(db, record)
    for channel in channels:
        explicit = intent.recipients.get(channel)
        recipients = (
            [explicit]
            if explicit
            else _subscriber_addresses(db, subscriber, channel, intent.category)
            if subscriber
            else []
        )
        if not recipients:
            add_decision(
                channel=channel,
                recipient=None,
                audience_type=intent.audience_type,
                audience_id=subscriber.id if subscriber else intent.audience_id,
                subscriber_id=subscriber.id if subscriber else None,
                reason="missing_address",
                persist_suppression=False,
                metadata=dict(intent.metadata),
            )
        for recipient in recipients:
            if intent.audience_type == "subscriber":
                policy = evaluate_customer_notification_policy(
                    db,
                    subscriber_id=subscriber.id if subscriber else None,
                    channel=channel,
                    category=intent.category,
                    event_type=intent.event_type,
                    recipient=recipient,
                    requested_status=intent.requested_status,
                )
                reason = policy.reason
            else:
                reason = suppression_reason(
                    db, channel=channel, category=intent.category, address=recipient
                )
            add_decision(
                channel=channel,
                recipient=recipient,
                audience_type=intent.audience_type,
                audience_id=subscriber.id if subscriber else intent.audience_id,
                subscriber_id=subscriber.id if subscriber else None,
                reason=reason,
                persist_suppression=(
                    intent.persist_policy_suppressions
                    and intent.audience_type == "subscriber"
                ),
                metadata=dict(intent.metadata),
            )
        reseller = subscriber.reseller if subscriber else None
        if (
            subscriber is None
            or not intent.include_reseller
            or reseller is None
            or reseller.is_house
            or not reseller.is_active
        ):
            continue
        for recipient in _reseller_addresses(db, reseller, channel):
            reason = suppression_reason(
                db, channel=channel, category=intent.category, address=recipient
            )
            add_decision(
                channel=channel,
                recipient=recipient,
                audience_type="reseller",
                audience_id=reseller.id,
                subscriber_id=None,
                reason=reason,
                persist_suppression=False,
                metadata={
                    **intent.metadata,
                    "subject_subscriber_id": str(subscriber.id),
                },
            )

    db.flush()
    record.suppression_reasons = suppressed
    if any(decision.decision == "accepted" for decision in record.recipient_decisions):
        record.status = "planned"
    else:
        record.status = "suppressed"
        record.processed_at = datetime.now(UTC)
    db.flush()
    _stage_plan_evidence(db, record)
    return _plan_from_record(db, record)


def recheck_planned_recipient(
    db: Session,
    decision_id: UUID,
    *,
    exclude_notification_id: UUID | None = None,
) -> PlannedRecipient:
    """Recheck an accepted recipient without reviving a suppressed decision."""
    from app.services.notification import evaluate_customer_notification_policy

    decision = (
        db.query(CommunicationIntentRecipient)
        .filter(CommunicationIntentRecipient.id == decision_id)
        .with_for_update()
        .one_or_none()
    )
    if decision is None or decision.tenant_id != operator_tenant_id():
        raise _intent_error(
            "decision_not_found", "Communication recipient decision not found"
        )
    if decision.decision != "accepted" or decision.recipient is None:
        return load_planned_recipient(db, decision_id)
    record = db.get(CommunicationIntentRecord, decision.intent_id)
    if record is None:
        raise _intent_error("intent_not_found", "Communication intent not found")
    reason: str | None = None
    if decision.audience_type == "subscriber":
        policy = evaluate_customer_notification_policy(
            db,
            subscriber_id=decision.subscriber_id,
            channel=decision.channel,
            category=record.category,
            event_type=record.event_type,
            recipient=decision.recipient,
            requested_status=decision.requested_status,
            exclude_notification_id=exclude_notification_id,
        )
        reason = policy.reason
    elif decision.audience_type == "reseller":
        reason = suppression_reason(
            db,
            channel=decision.channel,
            category=record.category,
            address=decision.recipient,
        )
    if reason:
        decision.decision = "suppressed"
        decision.suppression_reason = reason
        reasons = list(record.suppression_reasons or [])
        reasons.append(f"{decision.audience_type}:{decision.channel.value}:{reason}")
        record.suppression_reasons = reasons
        db.flush()
    return load_planned_recipient(db, decision_id)


def cover_planned_recipient(
    db: Session,
    decision_id: UUID,
    notification_id: UUID,
    *,
    newly_queued_notification: Notification | None = None,
) -> NotificationIntentCoverage:
    """Link a proved source recipient to an already composed physical delivery."""
    notification = newly_queued_notification
    if notification is not None and notification.id != notification_id:
        raise _intent_error(
            "coverage_conflict", "Physical notification identity changed"
        )
    if notification is None:
        notification = (
            db.query(Notification)
            .filter(Notification.id == notification_id)
            .with_for_update()
            .one_or_none()
        )
    if notification is None or notification.status is not NotificationStatus.queued:
        raise _intent_error(
            "notification_not_pending", "Physical notification must be pending"
        )
    decision = (
        db.query(CommunicationIntentRecipient)
        .filter(CommunicationIntentRecipient.id == decision_id)
        .with_for_update()
        .one_or_none()
    )
    if (
        decision is None
        or decision.tenant_id != operator_tenant_id()
        or decision.decision != "accepted"
    ):
        raise _intent_error("source_not_accepted", "Source recipient was not accepted")
    existing = (
        db.query(NotificationIntentCoverage)
        .filter(NotificationIntentCoverage.intent_recipient_id == decision.id)
        .one_or_none()
    )
    if existing is not None:
        if existing.notification_id != notification.id:
            raise _intent_error(
                "coverage_conflict",
                "Source recipient already covers a different delivery",
            )
        return existing
    record = db.get(CommunicationIntentRecord, decision.intent_id)
    if record is None:
        raise _intent_error("intent_not_found", "Communication intent not found")
    if (
        notification.channel != decision.channel
        or _normalized_recipient(notification.channel, notification.recipient)
        != decision.normalized_recipient
        or notification.audience_type != decision.audience_type
        or notification.audience_id != decision.audience_id
        or notification.subscriber_id != decision.subscriber_id
        or notification.category != record.category
        or (notification.metadata_ or {}).get("communication_class")
        != record.communication_class
    ):
        raise _intent_error(
            "coverage_identity_mismatch",
            "Physical delivery does not match source recipient identity",
        )
    if record.body and record.body not in (notification.body or ""):
        raise _intent_error(
            "coverage_content_mismatch",
            "Physical delivery does not contain source body",
        )
    required_attachments = {
        tuple(sorted(item.to_metadata().items())) for item in _attachment_refs(record)
    }
    actual_attachments = (notification.metadata_ or {}).get("attachments", [])
    actual = (
        {
            tuple(sorted(item.items()))
            for item in actual_attachments
            if isinstance(item, dict)
        }
        if isinstance(actual_attachments, list)
        else set()
    )
    if not required_attachments.issubset(actual):
        raise _intent_error(
            "coverage_content_mismatch", "Physical delivery omits source attachment"
        )
    if decision.canonical_send_at is not None:
        target = decision.canonical_send_at
        actual_send = notification.send_at
        if actual_send is None:
            raise _intent_error(
                "coverage_timing_mismatch",
                "Physical delivery precedes source timing policy",
            )
        target = target if target.tzinfo else target.replace(tzinfo=UTC)
        actual_send = (
            actual_send if actual_send.tzinfo else actual_send.replace(tzinfo=UTC)
        )
        if actual_send < target:
            raise _intent_error(
                "coverage_timing_mismatch",
                "Physical delivery precedes source timing policy",
            )
    coverage = NotificationIntentCoverage(
        tenant_id=operator_tenant_id(),
        intent_recipient_id=decision.id,
        notification_id=notification.id,
        status="covered",
    )
    db.add(coverage)
    db.flush()
    return coverage


def recheck_covered_notification(
    db: Session, notification_id: UUID
) -> CoveredNotificationRecheck:
    """Revalidate every covered source under the physical delivery lock.

    A source suppressed after planning remains evidence but never receives a
    delivered outcome merely because another source uses the same outbox row.
    The caller rebuilds the composed body from the returned eligible IDs.
    Queue, failed-retry, and in-progress claims may recheck; terminal delivery
    outcomes may not be changed by this operation.
    """
    notification = (
        db.query(Notification)
        .filter(Notification.id == notification_id)
        .with_for_update()
        .one_or_none()
    )
    if notification is None or notification.status not in {
        NotificationStatus.queued,
        NotificationStatus.failed,
        NotificationStatus.sending,
    }:
        raise _intent_error(
            "notification_not_pending",
            "Physical notification is not eligible for source recheck",
        )
    coverage_rows = (
        db.query(NotificationIntentCoverage)
        .filter(NotificationIntentCoverage.notification_id == notification.id)
        .filter(NotificationIntentCoverage.tenant_id == operator_tenant_id())
        .order_by(NotificationIntentCoverage.id)
        .with_for_update()
        .all()
    )
    eligible: list[UUID] = []
    suppressed: list[UUID] = []
    for coverage in coverage_rows:
        if coverage.status == "suppressed":
            suppressed.append(coverage.intent_recipient_id)
            continue
        planned = recheck_planned_recipient(
            db,
            coverage.intent_recipient_id,
            exclude_notification_id=notification.id,
        )
        if planned.accepted:
            eligible.append(planned.decision_id)
        else:
            coverage.status = "suppressed"
            suppressed.append(planned.decision_id)
    db.flush()
    return CoveredNotificationRecheck(
        notification_id=notification.id,
        eligible_decision_ids=tuple(eligible),
        suppressed_decision_ids=tuple(suppressed),
    )


def execute_planned_recipient(
    db: Session,
    decision_id: UUID,
    *,
    minimum_send_at: datetime | None = None,
) -> PlannedRecipientExecution:
    """Recheck and queue one source recipient through the existing queue owner."""
    from app.services.notification import notifications as notification_service

    # A replay may meet a provider claim that already holds Notification. Lock
    # that physical row first; a fresh execution has no physical row yet and
    # serializes on its decision instead.
    prior_coverage = (
        db.query(NotificationIntentCoverage)
        .filter(NotificationIntentCoverage.intent_recipient_id == decision_id)
        .one_or_none()
    )
    locked_notification = (
        db.query(Notification)
        .filter(Notification.id == prior_coverage.notification_id)
        .with_for_update()
        .one_or_none()
        if prior_coverage is not None
        else None
    )
    decision = (
        db.query(CommunicationIntentRecipient)
        .filter(CommunicationIntentRecipient.id == decision_id)
        .with_for_update()
        .one_or_none()
    )
    if decision is None or decision.tenant_id != operator_tenant_id():
        raise _intent_error(
            "decision_not_found", "Communication recipient decision not found"
        )
    existing = (
        db.query(NotificationIntentCoverage)
        .filter(NotificationIntentCoverage.intent_recipient_id == decision.id)
        .one_or_none()
    )
    if existing is not None:
        notification = (
            locked_notification
            if locked_notification is not None
            and locked_notification.id == existing.notification_id
            else db.get(Notification, existing.notification_id)
        )
        return PlannedRecipientExecution(
            decision_id=decision.id,
            notification_id=existing.notification_id,
            notification=notification,
            queued=existing.status == "covered"
            and decision.decision == "accepted"
            and notification is not None
            and notification.status == NotificationStatus.queued,
            suppression_reason=decision.suppression_reason,
        )
    planned = recheck_planned_recipient(db, decision.id)
    if planned.recipient is None or (
        not planned.accepted and not decision.persist_suppression
    ):
        return PlannedRecipientExecution(
            decision_id=decision.id,
            notification_id=None,
            notification=None,
            queued=False,
            suppression_reason=planned.suppression_reason,
        )
    record = db.get(CommunicationIntentRecord, decision.intent_id)
    if record is None:
        raise _intent_error("intent_not_found", "Communication intent not found")
    metadata = {
        **decision.metadata_,
        **(
            {"attachments": [item.to_metadata() for item in _attachment_refs(record)]}
            if _attachment_refs(record)
            else {}
        ),
        "communication_class": record.communication_class,
    }
    status = (
        decision.requested_status if planned.accepted else NotificationStatus.canceled
    )
    payload = NotificationCreate(
        template_id=record.template_id,
        subscriber_id=decision.subscriber_id,
        communication_intent_id=record.id,
        audience_type=decision.audience_type,
        audience_id=decision.audience_id,
        channel=decision.channel,
        event_type=record.event_type,
        category=record.category,
        recipient=planned.recipient,
        subject=record.subject,
        body=record.body,
        status=status,
        send_at=decision.send_at,
        last_error=decision.requested_last_error
        if planned.accepted
        else planned.suppression_reason,
        delivery_latency=NotificationDeliveryLatency(decision.delivery_latency),
        metadata_=metadata,
    )
    if decision.audience_type == "subscriber":
        notification = (
            notification_service.queue_customer_notification(
                db, payload, minimum_send_at=minimum_send_at
            )
            if decision.persist_suppression
            else notification_service.queue_event_notification(
                db, payload, minimum_send_at=minimum_send_at
            )
        )
    else:
        notification = notification_service.queue_internal_notification(
            db, payload, minimum_send_at=minimum_send_at
        )
    if notification is None:
        decision.decision = "suppressed"
        decision.suppression_reason = "customer_policy"
    elif notification.status == NotificationStatus.queued and planned.accepted:
        cover_planned_recipient(
            db,
            decision.id,
            notification.id,
            newly_queued_notification=notification,
        )
    elif notification.status == NotificationStatus.canceled and planned.accepted:
        decision.decision = "suppressed"
        decision.suppression_reason = notification.last_error or "customer_policy"
    db.flush()
    return PlannedRecipientExecution(
        decision_id=decision.id,
        notification_id=notification.id if notification else None,
        notification=notification,
        queued=notification is not None
        and notification.status == NotificationStatus.queued,
        suppression_reason=decision.suppression_reason,
    )


def _replay_deliveries(
    db: Session, record: CommunicationIntentRecord
) -> tuple[Notification, ...]:
    """Read physical deliveries credited to this source intent's recipients."""
    if not record.recipient_decisions:
        return tuple(record.notifications)
    # A composed Notification's compatibility FK names its first intent;
    # later source intents are linked only through accepted coverage.
    covered = (
        db.query(Notification)
        .join(
            NotificationIntentCoverage,
            NotificationIntentCoverage.notification_id == Notification.id,
        )
        .join(
            CommunicationIntentRecipient,
            CommunicationIntentRecipient.id
            == NotificationIntentCoverage.intent_recipient_id,
        )
        .filter(CommunicationIntentRecipient.intent_id == record.id)
        .filter(CommunicationIntentRecipient.tenant_id == operator_tenant_id())
        .filter(NotificationIntentCoverage.tenant_id == operator_tenant_id())
        .filter(CommunicationIntentRecipient.decision == "accepted")
        .filter(NotificationIntentCoverage.status == "covered")
        .all()
    )
    # Normal submit historically persists canceled policy decisions as
    # physical rows. Preserve those without crediting a later-suppressed
    # queued source as a live delivery.
    canceled = (
        row for row in record.notifications if row.status is NotificationStatus.canceled
    )
    return tuple({row.id: row for row in (*covered, *canceled)}.values())


def submit(
    db: Session,
    intent: CommunicationIntent,
    *,
    minimum_send_at: datetime | None = None,
) -> CommunicationIntentResult:
    """Keep the public API while using the same planner and queue execution."""
    plan = plan_intent(db, intent)
    record = db.get(CommunicationIntentRecord, plan.intent_id)
    if record is None:
        raise ValueError("Communication intent not found")
    if plan.replayed:
        notifications = _replay_deliveries(db, record)
        return CommunicationIntentResult(
            intent_id=record.id,
            deliveries=notifications,
            queued=tuple(
                row for row in notifications if row.status == NotificationStatus.queued
            ),
            suppressed=tuple(record.suppression_reasons or []),
            replayed=True,
        )
    deliveries: list[Notification] = []
    for recipient in plan.recipients:
        outcome = execute_planned_recipient(
            db, recipient.decision_id, minimum_send_at=minimum_send_at
        )
        if outcome.notification is not None:
            deliveries.append(outcome.notification)
    queued = tuple(row for row in deliveries if row.status == NotificationStatus.queued)
    record.status = (
        "partial"
        if queued and record.suppression_reasons
        else "expanded"
        if queued
        else "suppressed"
    )
    record.processed_at = datetime.now(UTC)
    db.flush()
    return CommunicationIntentResult(
        intent_id=record.id,
        deliveries=tuple(deliveries),
        queued=queued,
        suppressed=tuple(record.suppression_reasons or []),
    )


def record_delivery_outcome(db: Session, notification: Notification) -> None:
    """Project outbox delivery state into its intent, campaign, and inbox lineage."""
    from app.models.comms_campaign import (
        CampaignRecipient,
        CampaignRecipientStatus,
    )
    from app.models.team_inbox import InboxMessage

    db.flush()

    message = (
        db.query(InboxMessage)
        .filter(InboxMessage.notification_id == notification.id)
        .one_or_none()
    )
    if message is not None:
        metadata = dict(message.metadata_ or {})
        metadata["delivery_status"] = notification.status.value
        if notification.last_error:
            metadata["send_error"] = notification.last_error
        else:
            metadata.pop("send_error", None)
        message.metadata_ = metadata
        if notification.status == NotificationStatus.delivered:
            message.sent_at = notification.sent_at or datetime.now(UTC)
        # Realtime is an invalidation transport only. Publish the bounded
        # committed identity/status facts after this transaction completes;
        # the browser refetches the authoritative message projection on gaps.
        from app.services import team_inbox_realtime

        team_inbox_realtime.publish_conversation_event(
            db,
            str(message.conversation_id),
            event_type=team_inbox_realtime.EventType.MESSAGE_STATUS_CHANGED,
            payload={
                "conversation_id": str(message.conversation_id),
                "message_id": str(message.id),
                "delivery_status": notification.status.value,
            },
        )

    campaign_recipient = (
        db.query(CampaignRecipient)
        .filter(CampaignRecipient.notification_id == notification.id)
        .one_or_none()
    )
    if campaign_recipient is not None:
        if notification.status == NotificationStatus.delivered:
            campaign_recipient.status = CampaignRecipientStatus.delivered.value
            campaign_recipient.delivered_at = notification.sent_at or datetime.now(UTC)
            campaign_recipient.failed_reason = None
        elif (
            notification.status
            in {NotificationStatus.failed, NotificationStatus.bounced}
            and notification.send_at is None
        ):
            campaign_recipient.status = CampaignRecipientStatus.failed.value
            campaign_recipient.failed_reason = notification.last_error
        elif notification.status == NotificationStatus.canceled:
            campaign_recipient.status = CampaignRecipientStatus.skipped.value
            campaign_recipient.failed_reason = notification.last_error
        from app.services.comms_campaigns import refresh_campaign_delivery_state

        refresh_campaign_delivery_state(db, campaign_recipient.campaign_id)

    associated_intent_ids = {
        intent_id
        for (intent_id,) in (
            db.query(CommunicationIntentRecipient.intent_id)
            .join(
                NotificationIntentCoverage,
                NotificationIntentCoverage.intent_recipient_id
                == CommunicationIntentRecipient.id,
            )
            .filter(NotificationIntentCoverage.notification_id == notification.id)
            .all()
        )
    }
    if notification.communication_intent_id is not None:
        associated_intent_ids.add(notification.communication_intent_id)
    for intent_id in associated_intent_ids:
        intent_record = db.get(CommunicationIntentRecord, intent_id)
        if intent_record is None:
            continue
        decisions = (
            db.query(CommunicationIntentRecipient)
            .filter(CommunicationIntentRecipient.intent_id == intent_id)
            .all()
        )
        if decisions:
            # A primary Notification FK is compatibility only once recipient
            # decisions exist. It cannot override a suppressed source decision.
            delivery_rows = {
                row.id: row
                for row in (
                    db.query(Notification)
                    .join(
                        NotificationIntentCoverage,
                        NotificationIntentCoverage.notification_id == Notification.id,
                    )
                    .join(
                        CommunicationIntentRecipient,
                        CommunicationIntentRecipient.id
                        == NotificationIntentCoverage.intent_recipient_id,
                    )
                    .filter(CommunicationIntentRecipient.intent_id == intent_id)
                    .filter(CommunicationIntentRecipient.decision == "accepted")
                    .filter(NotificationIntentCoverage.status == "covered")
                    .all()
                )
            }
            suppressed_count = sum(
                decision.decision == "suppressed" for decision in decisions
            )
            accepted_count = len(decisions) - suppressed_count
        else:
            delivery_rows = {
                row.id: row
                for row in (
                    db.query(Notification)
                    .filter(Notification.communication_intent_id == intent_id)
                    .all()
                )
            }
            suppressed_count = 0
            accepted_count = 0
        states = {row.status for row in delivery_rows.values()}
        if states & {
            NotificationStatus.queued,
            NotificationStatus.sending,
            NotificationStatus.submitted,
        }:
            intent_record.status = "delivering"
        elif states and states <= {NotificationStatus.delivered}:
            intent_record.status = "partial" if suppressed_count else "delivered"
        elif any(
            row.status in {NotificationStatus.failed, NotificationStatus.bounced}
            and row.send_at is not None
            for row in delivery_rows.values()
        ):
            intent_record.status = "retrying"
        elif NotificationStatus.delivered in states:
            intent_record.status = "partial"
        elif states:
            intent_record.status = "failed"
        elif suppressed_count:
            intent_record.status = "suppressed"
        elif accepted_count:
            intent_record.status = "planned"
        intent_record.updated_at = datetime.now(UTC)
    db.flush()
