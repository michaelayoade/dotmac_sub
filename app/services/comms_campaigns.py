from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.comms_campaign import (
    Campaign,
    CampaignChannel,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignStatus,
    CampaignType,
)
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationStatus,
    InboxConversationTeam,
    InboxTeamRole,
    InboxTeamSource,
)
from app.services import team_inbox_outbound, team_inbox_routing
from app.services.common import coerce_uuid
from app.services.customer_identity_normalization import normalize_phone_identifier

NON_CONTACTABLE_STATUSES = {
    SubscriberStatus.disabled.value,
    SubscriberStatus.canceled.value,
}


@dataclass(frozen=True)
class CampaignAudienceBuildResult:
    campaign_id: UUID
    created: int
    skipped: int
    existing: int
    total_recipients: int
    skipped_reasons: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CampaignSendResult:
    campaign_id: UUID
    sent: int
    failed: int
    skipped: int
    completed: bool


def _now() -> datetime:
    return datetime.now(UTC)


def _campaign_or_404(db: Session, campaign_id: str | UUID) -> Campaign:
    campaign = db.get(Campaign, coerce_uuid(campaign_id))
    if campaign is None or not campaign.is_active:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


def _validate_campaign_values(campaign: Campaign) -> None:
    if campaign.channel not in {item.value for item in CampaignChannel}:
        raise HTTPException(status_code=400, detail="Invalid campaign channel")
    if campaign.campaign_type not in {item.value for item in CampaignType}:
        raise HTTPException(status_code=400, detail="Invalid campaign type")
    if campaign.channel == CampaignChannel.email.value and not (
        campaign.body_html or campaign.body_text
    ):
        raise HTTPException(status_code=400, detail="Email body is required")
    if campaign.channel == CampaignChannel.email.value and not campaign.subject:
        raise HTTPException(status_code=400, detail="Email subject is required")
    if campaign.channel == CampaignChannel.whatsapp.value and not (
        campaign.body_text or campaign.whatsapp_template_name
    ):
        raise HTTPException(
            status_code=400,
            detail="WhatsApp campaign needs body_text or a template name",
        )


def _segment_query(db: Session, campaign: Campaign):
    segment = (
        campaign.segment_filter if isinstance(campaign.segment_filter, dict) else {}
    )
    query = db.query(Subscriber).outerjoin(
        Reseller, Subscriber.reseller_id == Reseller.id
    )
    query = query.filter(Subscriber.is_active.is_(True))
    query = query.filter(Subscriber.status.notin_(NON_CONTACTABLE_STATUSES))
    query = query.filter(
        or_(Subscriber.reseller_id.is_(None), Reseller.is_active.is_(True))
    )

    raw_status = segment.get("status")
    if raw_status:
        statuses = raw_status if isinstance(raw_status, list) else [raw_status]
        query = query.filter(Subscriber.status.in_([str(item) for item in statuses]))

    raw_reseller_id = segment.get("reseller_id")
    if raw_reseller_id:
        query = query.filter(Subscriber.reseller_id == coerce_uuid(raw_reseller_id))

    raw_category = segment.get("subscriber_category") or segment.get("category")
    if raw_category:
        categories = raw_category if isinstance(raw_category, list) else [raw_category]
        category_values = [str(item).strip().lower() for item in categories if item]
        if category_values:
            query = query.filter(
                Subscriber.metadata_["subscriber_category"].astext.in_(category_values)
            )

    raw_billing_mode = segment.get("billing_mode")
    if raw_billing_mode:
        modes = (
            raw_billing_mode
            if isinstance(raw_billing_mode, list)
            else [raw_billing_mode]
        )
        query = query.filter(Subscriber.billing_mode.in_([str(item) for item in modes]))

    if bool(segment.get("marketing_opt_in_only")):
        query = query.filter(Subscriber.marketing_opt_in.is_(True))

    limit = segment.get("limit")
    if isinstance(limit, int) and limit > 0:
        query = query.limit(limit)
    return query


def _recipient_address(
    campaign: Campaign, subscriber: Subscriber
) -> tuple[str | None, str | None]:
    if campaign.channel == CampaignChannel.email.value:
        email = team_inbox_routing.normalize_email_address(subscriber.email)
        return email, email
    if campaign.channel == CampaignChannel.whatsapp.value:
        phone = normalize_phone_identifier(subscriber.phone)
        return phone, None
    return None, None


def create_campaign(
    db: Session, payload, *, created_by_system_user_id: str | UUID | None = None
) -> Campaign:
    campaign = Campaign(
        name=payload.name,
        campaign_type=payload.campaign_type,
        channel=payload.channel,
        status=CampaignStatus.scheduled.value
        if payload.scheduled_at
        else CampaignStatus.draft.value,
        subject=payload.subject,
        body_html=payload.body_html,
        body_text=payload.body_text,
        whatsapp_template_name=payload.whatsapp_template_name,
        whatsapp_template_language=payload.whatsapp_template_language,
        whatsapp_template_components=payload.whatsapp_template_components,
        segment_filter=payload.segment_filter or {},
        scheduled_at=payload.scheduled_at,
        created_by_system_user_id=coerce_uuid(created_by_system_user_id),
        service_team_id=payload.service_team_id,
        connector_config_id=payload.connector_config_id,
        metadata_=payload.metadata or {},
    )
    _validate_campaign_values(campaign)
    db.add(campaign)
    db.flush()
    return campaign


def update_campaign(db: Session, campaign_id: str | UUID, payload) -> Campaign:
    campaign = _campaign_or_404(db, campaign_id)
    if campaign.status == CampaignStatus.sending.value:
        raise HTTPException(
            status_code=409, detail="Sending campaigns cannot be edited"
        )
    for field_name, value in payload.model_dump(exclude_unset=True).items():
        if field_name == "metadata":
            campaign.metadata_ = value or {}
        else:
            setattr(campaign, field_name, value)
    _validate_campaign_values(campaign)
    db.flush()
    return campaign


def list_campaigns(
    db: Session,
    *,
    status: str | None = None,
    channel: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Campaign]:
    query = db.query(Campaign).filter(Campaign.is_active.is_(True))
    if status:
        query = query.filter(Campaign.status == status)
    if channel:
        query = query.filter(Campaign.channel == channel)
    return (
        query.order_by(Campaign.created_at.desc(), Campaign.id.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )


def list_campaign_recipients(
    db: Session,
    campaign_id: str | UUID,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[CampaignRecipient]:
    query = db.query(CampaignRecipient).filter(
        CampaignRecipient.campaign_id == coerce_uuid(campaign_id)
    )
    if status:
        query = query.filter(CampaignRecipient.status == status)
    return (
        query.order_by(CampaignRecipient.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )


def build_recipient_list(
    db: Session,
    campaign_id: str | UUID,
    *,
    limit: int | None = None,
) -> CampaignAudienceBuildResult:
    campaign = _campaign_or_404(db, campaign_id)
    _validate_campaign_values(campaign)
    created = 0
    existing = 0
    skipped: Counter[str] = Counter()
    query = _segment_query(db, campaign)
    if limit is not None and limit > 0:
        query = query.limit(limit)

    for subscriber in query.all():
        address, email = _recipient_address(campaign, subscriber)
        if not address:
            skipped["missing_address"] += 1
            continue
        if (
            subscriber.status.value in NON_CONTACTABLE_STATUSES
            or not subscriber.is_active
        ):
            skipped["inactive_subscriber"] += 1
            continue
        if subscriber.reseller is not None and not subscriber.reseller.is_active:
            skipped["inactive_reseller"] += 1
            continue
        already_exists = (
            db.query(CampaignRecipient.id)
            .filter(CampaignRecipient.campaign_id == campaign.id)
            .filter(CampaignRecipient.subscriber_id == subscriber.id)
            .filter(CampaignRecipient.step_id.is_(None))
            .first()
            is not None
        )
        if already_exists:
            existing += 1
            continue
        recipient = CampaignRecipient(
            campaign_id=campaign.id,
            subscriber_id=subscriber.id,
            address=address,
            email=email,
            status=CampaignRecipientStatus.pending.value,
            metadata_={"source": "native_campaign_audience"},
        )
        db.add(recipient)
        db.flush()
        created += 1

    campaign.total_recipients = (
        db.query(CampaignRecipient)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .filter(CampaignRecipient.status != CampaignRecipientStatus.skipped.value)
        .count()
    )
    metadata = dict(campaign.metadata_ or {})
    metadata["last_audience_build"] = {
        "created": created,
        "existing": existing,
        "skipped": sum(skipped.values()),
        "skipped_reasons": dict(skipped),
        "built_at": _now().isoformat(),
    }
    campaign.metadata_ = metadata
    db.flush()
    return CampaignAudienceBuildResult(
        campaign_id=campaign.id,
        created=created,
        skipped=sum(skipped.values()),
        existing=existing,
        total_recipients=campaign.total_recipients,
        skipped_reasons=dict(skipped),
    )


def _conversation_for_recipient(
    db: Session,
    *,
    campaign: Campaign,
    recipient: CampaignRecipient,
    now: datetime,
) -> InboxConversation:
    external_thread_id = f"campaign:{campaign.id}:{recipient.subscriber_id}"
    conversation = (
        db.query(InboxConversation)
        .filter(InboxConversation.channel_type == campaign.channel)
        .filter(InboxConversation.external_thread_id == external_thread_id)
        .one_or_none()
    )
    if conversation is not None:
        if conversation.status == InboxConversationStatus.resolved.value:
            conversation.status = InboxConversationStatus.open.value
        return conversation

    conversation = InboxConversation(
        subscriber_id=recipient.subscriber_id,
        primary_service_team_id=campaign.service_team_id,
        channel_type=campaign.channel,
        status=InboxConversationStatus.open.value,
        subject=campaign.subject or campaign.name,
        contact_address=recipient.address,
        external_thread_id=external_thread_id,
        first_message_at=now,
        last_message_at=now,
        metadata_={
            "source": "native_campaign",
            "campaign_id": str(campaign.id),
            "campaign_recipient_id": str(recipient.id),
        },
    )
    db.add(conversation)
    db.flush()
    if campaign.service_team_id is not None:
        db.add(
            InboxConversationTeam(
                conversation_id=conversation.id,
                service_team_id=campaign.service_team_id,
                role=InboxTeamRole.owner.value,
                source=InboxTeamSource.manual.value,
                metadata_={"source": "native_campaign"},
            )
        )
    db.flush()
    return conversation


def _payload_for_recipient(campaign: Campaign, recipient: CampaignRecipient):
    metadata: dict[str, object] = {
        "source_route": "native_campaign",
        "campaign_id": str(campaign.id),
        "campaign_recipient_id": str(recipient.id),
    }
    if (
        campaign.channel == CampaignChannel.whatsapp.value
        and campaign.whatsapp_template_name
    ):
        metadata["whatsapp_template"] = {
            "name": campaign.whatsapp_template_name,
            "language": campaign.whatsapp_template_language or "en",
            "variables": campaign.whatsapp_template_components or {},
        }
    return team_inbox_outbound.InboxReplyPayload(
        body_html=campaign.body_html or campaign.body_text or "",
        body_text=campaign.body_text,
        subject=campaign.subject or campaign.name,
        to_email=recipient.email,
        metadata=metadata,
    )


def send_campaign_batch(
    db: Session,
    campaign_id: str | UUID,
    *,
    batch_size: int = 100,
    now: datetime | None = None,
) -> CampaignSendResult:
    campaign = _campaign_or_404(db, campaign_id)
    _validate_campaign_values(campaign)
    current_time = now or _now()
    if campaign.status in {
        CampaignStatus.canceled.value,
        CampaignStatus.completed.value,
    }:
        raise HTTPException(status_code=409, detail="Campaign is not sendable")
    if campaign.sending_started_at is None:
        campaign.sending_started_at = current_time
    campaign.status = CampaignStatus.sending.value

    recipients = (
        db.query(CampaignRecipient)
        .join(Subscriber, CampaignRecipient.subscriber_id == Subscriber.id)
        .outerjoin(Reseller, Subscriber.reseller_id == Reseller.id)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .filter(CampaignRecipient.status == CampaignRecipientStatus.pending.value)
        .filter(Subscriber.is_active.is_(True))
        .filter(Subscriber.status.notin_(NON_CONTACTABLE_STATUSES))
        .filter(or_(Subscriber.reseller_id.is_(None), Reseller.is_active.is_(True)))
        .order_by(CampaignRecipient.created_at.asc(), CampaignRecipient.id.asc())
        .limit(batch_size)
        .all()
    )

    sent = failed = skipped = 0
    for recipient in recipients:
        conversation = _conversation_for_recipient(
            db, campaign=campaign, recipient=recipient, now=current_time
        )
        result = team_inbox_outbound.send_inbox_reply(
            db,
            conversation=conversation,
            payload=_payload_for_recipient(campaign, recipient),
            now=current_time,
            record_failure=True,
        )
        recipient.conversation_id = conversation.id
        if result.kind == "sent" and result.message_id is not None:
            recipient.status = CampaignRecipientStatus.sent.value
            recipient.sent_at = current_time
            recipient.message_id = coerce_uuid(result.message_id)
            sent += 1
        elif result.kind in {"missing_recipient", "empty_body"}:
            recipient.status = CampaignRecipientStatus.skipped.value
            recipient.failed_reason = result.reason
            skipped += 1
        else:
            recipient.status = CampaignRecipientStatus.failed.value
            recipient.failed_reason = result.reason or result.kind
            if result.message_id is not None:
                recipient.message_id = coerce_uuid(result.message_id)
            failed += 1

    _refresh_counts(db, campaign)
    pending = (
        db.query(CampaignRecipient)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .filter(CampaignRecipient.status == CampaignRecipientStatus.pending.value)
        .count()
    )
    completed = pending == 0
    if completed:
        campaign.status = CampaignStatus.completed.value
        campaign.completed_at = current_time
    db.flush()
    return CampaignSendResult(
        campaign_id=campaign.id,
        sent=sent,
        failed=failed,
        skipped=skipped,
        completed=completed,
    )


def _refresh_counts(db: Session, campaign: Campaign) -> None:
    counts = Counter(
        status
        for (status,) in db.query(CampaignRecipient.status)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .all()
    )
    campaign.total_recipients = sum(counts.values()) - counts.get(
        CampaignRecipientStatus.skipped.value, 0
    )
    campaign.sent_count = counts.get(CampaignRecipientStatus.sent.value, 0)
    campaign.delivered_count = counts.get(CampaignRecipientStatus.delivered.value, 0)
    campaign.failed_count = counts.get(CampaignRecipientStatus.failed.value, 0)
    campaign.opened_count = counts.get(CampaignRecipientStatus.opened.value, 0)
    campaign.clicked_count = counts.get(CampaignRecipientStatus.clicked.value, 0)


def process_due_campaigns(
    db: Session, *, now: datetime | None = None, limit: int = 20
) -> dict[str, int]:
    current_time = now or _now()
    campaigns = (
        db.query(Campaign)
        .filter(Campaign.is_active.is_(True))
        .filter(Campaign.status == CampaignStatus.scheduled.value)
        .filter(
            and_(
                Campaign.scheduled_at.is_not(None),
                Campaign.scheduled_at <= current_time,
            )
        )
        .order_by(Campaign.scheduled_at.asc())
        .limit(limit)
        .all()
    )
    built = sent = failed = 0
    for campaign in campaigns:
        try:
            build_recipient_list(db, campaign.id)
            result = send_campaign_batch(db, campaign.id, now=current_time)
            built += 1
            sent += result.sent
            failed += result.failed
        except Exception:
            campaign.status = CampaignStatus.failed.value
            metadata = dict(campaign.metadata_ or {})
            metadata["last_processing_error_at"] = current_time.isoformat()
            campaign.metadata_ = metadata
            failed += 1
    return {"campaigns": len(campaigns), "built": built, "sent": sent, "failed": failed}
