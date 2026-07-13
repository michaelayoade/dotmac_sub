from __future__ import annotations

import html
import re
import secrets
import zoneinfo
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.comms_campaign import (
    Campaign,
    CampaignChannel,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignSender,
    CampaignSmtpConfig,
    CampaignStatus,
    CampaignStep,
    CampaignSuppression,
    CampaignSuppressionReason,
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
from app.models.notification import (
    CommunicationSuppression,
    SuppressionReason,
    SuppressionScope,
)
from app.services import (
    communication_eligibility,
    team_inbox_outbound,
    team_inbox_routing,
)
from app.services.common import coerce_uuid
from app.services.customer_identity_normalization import normalize_phone_identifier

NON_CONTACTABLE_STATUSES = {
    SubscriberStatus.disabled.value,
    SubscriberStatus.canceled.value,
}

DEFAULT_SEND_WINDOW_TIMEZONE = "Africa/Lagos"
UNSUBSCRIBE_PATH = "/api/v1/campaigns/public/unsubscribe"

# Recipient states that must never be handed to the transport again.
TERMINAL_RECIPIENT_STATUSES = {
    CampaignRecipientStatus.suppressed.value,
    CampaignRecipientStatus.skipped.value,
}

_VARIABLE_PATTERN = re.compile(r"\{\{\s*(\w+)\s*\}\}")


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
    suppressed: int = 0


@dataclass(frozen=True)
class CampaignStepMaterializeResult:
    campaign_id: UUID
    step_id: UUID
    created: int
    suppressed: int


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


#: Everything a campaign sends is marketing. This is what makes an unsubscribe
#: actually stop it -- a marketing-scoped suppression blocks this category and
#: leaves invoices alone.
MARKETING_CATEGORY = "marketing"


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

    # Marketing consent is NOT a segment option. It used to be
    # `if segment.get("marketing_opt_in_only")` -- untick the box and the
    # campaign went to people who never opted in. Consent is not a filter you
    # choose to apply; it is the precondition for a campaign existing.
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


# ---------------------------------------------------------------------------
# Suppression / unsubscribe
#
# Suppression is the one control in this module that is correctness-critical:
# a suppressed address must never receive a campaign message. It is enforced
# twice on purpose — once when the audience is built (so the list is honest)
# and again immediately before each send (because an unsubscribe can land
# between the build and the send).
# ---------------------------------------------------------------------------
















def unsubscribe_by_token(
    db: Session, token: str, *, source: str = "unsubscribe_link"
) -> CommunicationSuppression:
    """Honor a one-click unsubscribe link.

    The token is minted per recipient row, so it identifies both the address to
    suppress and the campaign that prompted it.

    This writes to the PLATFORM ledger, not a campaign-local table. That is the
    whole point: an unsubscribe must silence the customer everywhere, not just
    in the campaign module that happened to carry the link. It is scoped to
    ``marketing`` -- the customer refused promotions, not their invoice.
    """
    clean_token = (token or "").strip()
    if not clean_token:
        raise HTTPException(status_code=404, detail="Unknown unsubscribe token")
    recipient = (
        db.query(CampaignRecipient)
        .filter(CampaignRecipient.unsubscribe_token == clean_token)
        .first()
    )
    if recipient is None:
        raise HTTPException(status_code=404, detail="Unknown unsubscribe token")
    campaign = db.get(Campaign, recipient.campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Unknown unsubscribe token")

    suppression = communication_eligibility.suppress(
        db,
        channel=campaign.channel,
        address=recipient.address,
        scope=SuppressionScope.marketing,
        reason=SuppressionReason.unsubscribe,
        subscriber_id=recipient.subscriber_id,
        note=f"campaign={campaign.id} source={source}",
        created_by=source,
    )
    # Stop the ones already queued for this campaign. The delivery gate would
    # catch them anyway -- this just keeps the campaign's own counts honest.
    (
        db.query(CampaignRecipient)
        .filter(CampaignRecipient.address == recipient.address)
        .filter(
            CampaignRecipient.status.in_(
                [
                    CampaignRecipientStatus.pending.value,
                    CampaignRecipientStatus.queued.value,
                ]
            )
        )
        .update(
            {CampaignRecipient.status: CampaignRecipientStatus.suppressed.value},
            synchronize_session=False,
        )
    )
    return suppression




# ---------------------------------------------------------------------------
# Sender profiles and SMTP configuration
# ---------------------------------------------------------------------------


def list_senders(
    db: Session, *, is_active: bool | None = None, limit: int = 50, offset: int = 0
) -> list[CampaignSender]:
    query = db.query(CampaignSender)
    if is_active is not None:
        query = query.filter(CampaignSender.is_active.is_(is_active))
    return query.order_by(CampaignSender.name.asc()).limit(limit).offset(offset).all()


def _sender_or_404(db: Session, sender_id: str | UUID) -> CampaignSender:
    sender = db.get(CampaignSender, coerce_uuid(sender_id))
    if sender is None:
        raise HTTPException(status_code=404, detail="Campaign sender not found")
    return sender


def _smtp_config_or_404(db: Session, smtp_config_id: str | UUID) -> CampaignSmtpConfig:
    config = db.get(CampaignSmtpConfig, coerce_uuid(smtp_config_id))
    if config is None:
        raise HTTPException(status_code=404, detail="SMTP configuration not found")
    return config


def create_sender(db: Session, payload) -> CampaignSender:
    if payload.campaign_smtp_config_id is not None:
        _smtp_config_or_404(db, payload.campaign_smtp_config_id)
    sender = CampaignSender(
        name=payload.name,
        sender_key=payload.sender_key.strip().lower(),
        from_name=payload.from_name,
        from_email=payload.from_email,
        reply_to=payload.reply_to,
        campaign_smtp_config_id=payload.campaign_smtp_config_id,
        is_active=payload.is_active,
        metadata_=payload.metadata or {},
    )
    db.add(sender)
    db.flush()
    return sender


def update_sender(db: Session, sender_id: str | UUID, payload) -> CampaignSender:
    sender = _sender_or_404(db, sender_id)
    for field_name, value in payload.model_dump(exclude_unset=True).items():
        if field_name == "metadata":
            sender.metadata_ = value or {}
        elif field_name == "campaign_smtp_config_id":
            if value is not None:
                _smtp_config_or_404(db, value)
            sender.campaign_smtp_config_id = value
        elif field_name == "sender_key" and value is not None:
            sender.sender_key = value.strip().lower()
        else:
            setattr(sender, field_name, value)
    db.flush()
    return sender


def list_smtp_configs(
    db: Session, *, is_active: bool | None = None, limit: int = 50, offset: int = 0
) -> list[CampaignSmtpConfig]:
    query = db.query(CampaignSmtpConfig)
    if is_active is not None:
        query = query.filter(CampaignSmtpConfig.is_active.is_(is_active))
    return (
        query.order_by(CampaignSmtpConfig.name.asc()).limit(limit).offset(offset).all()
    )


def create_smtp_config(db: Session, payload) -> CampaignSmtpConfig:
    config = CampaignSmtpConfig(
        name=payload.name,
        host=payload.host,
        port=payload.port,
        username=payload.username,
        password=payload.password,
        use_tls=payload.use_tls,
        use_ssl=payload.use_ssl,
        is_active=payload.is_active,
        metadata_=payload.metadata or {},
    )
    db.add(config)
    db.flush()
    return config


def update_smtp_config(
    db: Session, smtp_config_id: str | UUID, payload
) -> CampaignSmtpConfig:
    config = _smtp_config_or_404(db, smtp_config_id)
    for field_name, value in payload.model_dump(exclude_unset=True).items():
        if field_name == "metadata":
            config.metadata_ = value or {}
        elif field_name == "password":
            # An omitted password keeps the stored one; an explicit null clears it.
            config.password = value
        else:
            setattr(config, field_name, value)
    db.flush()
    return config


def _smtp_overlay(config: CampaignSmtpConfig) -> dict[str, object]:
    return {
        "host": config.host,
        "port": config.port,
        "username": config.username,
        "password": config.password,
        "use_tls": config.use_tls,
        "use_ssl": config.use_ssl,
    }


def resolve_sender_override(
    db: Session, campaign: Campaign
) -> team_inbox_outbound.OutboundSenderOverride | None:
    """Turn the campaign's sender profile + SMTP config into a transport override.

    Precedence: the campaign's own SMTP config beats the sender profile's, and
    both beat the team-resolved default that `team_inbox_outbound` would pick.
    """
    sender: CampaignSender | None = None
    if campaign.campaign_sender_id is not None:
        sender = db.get(CampaignSender, campaign.campaign_sender_id)
        if sender is not None and not sender.is_active:
            sender = None

    smtp_config: CampaignSmtpConfig | None = None
    if campaign.campaign_smtp_config_id is not None:
        smtp_config = db.get(CampaignSmtpConfig, campaign.campaign_smtp_config_id)
    elif sender is not None and sender.campaign_smtp_config_id is not None:
        smtp_config = db.get(CampaignSmtpConfig, sender.campaign_smtp_config_id)
    if smtp_config is not None and not smtp_config.is_active:
        smtp_config = None

    from_name = campaign.from_name or (sender.from_name if sender else None)
    from_email = campaign.from_email or (sender.from_email if sender else None)
    reply_to = campaign.reply_to or (sender.reply_to if sender else None)
    sender_key = sender.sender_key if sender else None

    if not any([from_name, from_email, reply_to, sender_key, smtp_config]):
        return None
    return team_inbox_outbound.OutboundSenderOverride(
        sender_key=sender_key,
        from_name=from_name,
        from_email=from_email,
        reply_to=reply_to,
        smtp=_smtp_overlay(smtp_config) if smtp_config is not None else None,
    )


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def list_campaign_steps(db: Session, campaign_id: str | UUID) -> list[CampaignStep]:
    return (
        db.query(CampaignStep)
        .filter(CampaignStep.campaign_id == coerce_uuid(campaign_id))
        .order_by(CampaignStep.step_index.asc())
        .all()
    )


def _step_or_404(
    db: Session, campaign_id: str | UUID, step_id: str | UUID
) -> CampaignStep:
    step = db.get(CampaignStep, coerce_uuid(step_id))
    if step is None or step.campaign_id != coerce_uuid(campaign_id):
        raise HTTPException(status_code=404, detail="Campaign step not found")
    return step


def _validate_step_content(campaign: Campaign, step: CampaignStep) -> None:
    if campaign.channel != CampaignChannel.email.value:
        return
    if not (
        step.body_html or step.body_text or campaign.body_html or campaign.body_text
    ):
        raise HTTPException(status_code=400, detail="Step body is required")
    if not (step.subject or campaign.subject):
        raise HTTPException(status_code=400, detail="Step subject is required")


def create_campaign_step(db: Session, campaign_id: str | UUID, payload) -> CampaignStep:
    campaign = _campaign_or_404(db, campaign_id)
    if campaign.status == CampaignStatus.sending.value:
        raise HTTPException(
            status_code=409, detail="Sending campaigns cannot be edited"
        )
    step_index = payload.step_index
    if step_index is None:
        highest = (
            db.query(CampaignStep.step_index)
            .filter(CampaignStep.campaign_id == campaign.id)
            .order_by(CampaignStep.step_index.desc())
            .first()
        )
        step_index = 0 if highest is None else int(highest[0]) + 1
    clash = (
        db.query(CampaignStep.id)
        .filter(CampaignStep.campaign_id == campaign.id)
        .filter(CampaignStep.step_index == step_index)
        .first()
    )
    if clash is not None:
        raise HTTPException(status_code=409, detail="Step index already used")

    step = CampaignStep(
        campaign_id=campaign.id,
        step_index=step_index,
        name=payload.name,
        subject=payload.subject,
        body_html=payload.body_html,
        body_text=payload.body_text,
        delay_days=payload.delay_days,
        delay_hours=payload.delay_hours,
        is_active=payload.is_active,
    )
    _validate_step_content(campaign, step)
    db.add(step)
    db.flush()
    return step


def update_campaign_step(
    db: Session, campaign_id: str | UUID, step_id: str | UUID, payload
) -> CampaignStep:
    campaign = _campaign_or_404(db, campaign_id)
    step = _step_or_404(db, campaign.id, step_id)
    for field_name, value in payload.model_dump(exclude_unset=True).items():
        setattr(step, field_name, value)
    _validate_step_content(campaign, step)
    db.flush()
    return step


def delete_campaign_step(
    db: Session, campaign_id: str | UUID, step_id: str | UUID
) -> None:
    campaign = _campaign_or_404(db, campaign_id)
    step = _step_or_404(db, campaign.id, step_id)
    sent = (
        db.query(CampaignRecipient.id)
        .filter(CampaignRecipient.step_id == step.id)
        .first()
    )
    if sent is not None:
        raise HTTPException(
            status_code=409, detail="Step already has recipients and cannot be deleted"
        )
    db.delete(step)
    db.flush()


# ---------------------------------------------------------------------------
# Send windows
# ---------------------------------------------------------------------------


def _send_window_timezone(campaign: Campaign) -> zoneinfo.ZoneInfo:
    name = (campaign.send_window_timezone or DEFAULT_SEND_WINDOW_TIMEZONE).strip()
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return zoneinfo.ZoneInfo(DEFAULT_SEND_WINDOW_TIMEZONE)


def within_send_window(campaign: Campaign, now: datetime) -> bool:
    """True when `now` falls inside the campaign's local send window.

    A campaign without a window is always sendable. Windows that wrap midnight
    (start > end, e.g. 20:00 -> 06:00) are supported.
    """
    start = campaign.send_window_start_hour
    end = campaign.send_window_end_hour
    if start is None or end is None:
        return True
    if start == end:
        return True
    local_hour = now.astimezone(_send_window_timezone(campaign)).hour
    if start < end:
        return start <= local_hour < end
    return local_hour >= start or local_hour < end


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
        send_window_start_hour=payload.send_window_start_hour,
        send_window_end_hour=payload.send_window_end_hour,
        send_window_timezone=payload.send_window_timezone,
        created_by_system_user_id=coerce_uuid(created_by_system_user_id),
        campaign_sender_id=payload.campaign_sender_id,
        campaign_smtp_config_id=payload.campaign_smtp_config_id,
        service_team_id=payload.service_team_id,
        connector_config_id=payload.connector_config_id,
        metadata_=payload.metadata or {},
    )
    if payload.campaign_sender_id is not None:
        _sender_or_404(db, payload.campaign_sender_id)
    if payload.campaign_smtp_config_id is not None:
        _smtp_config_or_404(db, payload.campaign_smtp_config_id)
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

    subscribers = query.all()
    # Suppression is checked in bulk here so a large audience build stays one
    # extra query rather than one per candidate.
    blocked = suppressed_addresses(
        db,
        channel=campaign.channel,
        addresses=[_recipient_address(campaign, item)[0] for item in subscribers],
    )

    for subscriber in subscribers:
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
        if address in blocked:
            skipped["suppressed"] += 1
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
            unsubscribe_token=_new_unsubscribe_token(),
            metadata_={"source": "native_campaign_audience"},
        )
        db.add(recipient)
        db.flush()
        created += 1

    campaign.total_recipients = _countable_recipients(db, campaign)
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


def _new_unsubscribe_token() -> str:
    return secrets.token_urlsafe(32)[:64]


def _countable_recipients(db: Session, campaign: Campaign) -> int:
    """Recipients that count towards the campaign audience.

    Skipped and suppressed rows are excluded: neither was, nor will be, mailed.
    """
    return (
        db.query(CampaignRecipient)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .filter(CampaignRecipient.status.notin_(sorted(TERMINAL_RECIPIENT_STATUSES)))
        .count()
    )


def unsubscribe_url(db: Session, recipient: CampaignRecipient) -> str | None:
    if not recipient.unsubscribe_token:
        return None
    from app.services.email import _get_app_url

    base = (_get_app_url(db) or "").rstrip("/")
    return f"{base}{UNSUBSCRIBE_PATH}/{recipient.unsubscribe_token}"


def _render_template(template: str | None, variables: dict[str, str]) -> str | None:
    if not template:
        return template

    def _replace(match: re.Match[str]) -> str:
        return variables.get(match.group(1), match.group(0))

    return _VARIABLE_PATTERN.sub(_replace, template)


def _step_content(
    campaign: Campaign, step: CampaignStep | None
) -> tuple[str | None, str | None, str | None]:
    """Subject/html/text for this send, with step content overriding the campaign."""
    if step is None:
        return campaign.subject, campaign.body_html, campaign.body_text
    subject = step.subject or campaign.subject
    body_html = step.body_html or (None if step.body_text else campaign.body_html)
    body_text = step.body_text or (None if step.body_html else campaign.body_text)
    return subject, body_html, body_text


def _append_unsubscribe_footer(
    body_html: str | None, body_text: str | None, url: str
) -> tuple[str | None, str | None]:
    escaped = html.escape(url, quote=True)
    footer_html = (
        f'<p style="font-size:12px;color:#888">'
        f'<a href="{escaped}">Unsubscribe from these emails</a></p>'
    )
    return (
        f"{body_html}{footer_html}" if body_html else body_html,
        f"{body_text}\n\nUnsubscribe: {url}" if body_text else body_text,
    )


def _payload_for_recipient(
    db: Session,
    campaign: Campaign,
    recipient: CampaignRecipient,
    *,
    step: CampaignStep | None = None,
    sender_override: team_inbox_outbound.OutboundSenderOverride | None = None,
):
    metadata: dict[str, object] = {
        "source_route": "native_campaign",
        "campaign_id": str(campaign.id),
        "campaign_recipient_id": str(recipient.id),
    }
    if step is not None:
        metadata["campaign_step_id"] = str(step.id)
        metadata["campaign_step_index"] = step.step_index
    if (
        campaign.channel == CampaignChannel.whatsapp.value
        and campaign.whatsapp_template_name
    ):
        metadata["whatsapp_template"] = {
            "name": campaign.whatsapp_template_name,
            "language": campaign.whatsapp_template_language or "en",
            "variables": campaign.whatsapp_template_components or {},
        }

    subject, body_html, body_text = _step_content(campaign, step)
    subscriber = recipient.subscriber
    link = unsubscribe_url(db, recipient) or ""
    variables = {
        "first_name": getattr(subscriber, "first_name", None) or "",
        "last_name": getattr(subscriber, "last_name", None) or "",
        "email": recipient.email or "",
        "unsubscribe_url": link,
        "campaign_name": campaign.name,
    }
    subject = _render_template(subject, variables)
    body_html = _render_template(body_html, variables)
    body_text = _render_template(body_text, variables)

    # Every marketing email carries an unsubscribe path. If the template did not
    # place the link itself, append a footer rather than ship a mail with no way out.
    if (
        campaign.channel == CampaignChannel.email.value
        and link
        and link not in (body_html or "")
        and link not in (body_text or "")
    ):
        body_html, body_text = _append_unsubscribe_footer(body_html, body_text, link)

    return team_inbox_outbound.InboxReplyPayload(
        body_html=body_html or body_text or "",
        body_text=body_text,
        subject=subject or campaign.name,
        to_email=recipient.email,
        metadata=metadata,
        sender_override=sender_override,
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

    # Re-check suppression at send time, not just at audience-build time: an
    # unsubscribe can land in between, and a suppressed address must never be
    # handed to the transport.
    blocked = suppressed_addresses(
        db,
        channel=campaign.channel,
        addresses=[recipient.address for recipient in recipients],
    )
    sender_override = resolve_sender_override(db, campaign)
    steps = {step.id: step for step in list_campaign_steps(db, campaign.id)}

    sent = failed = skipped = suppressed = 0
    for recipient in recipients:
        if recipient.address in blocked:
            recipient.status = CampaignRecipientStatus.suppressed.value
            recipient.suppressed_at = current_time
            recipient.failed_reason = "Address is on the campaign suppression list"
            suppressed += 1
            continue

        recipient.attempt_count += 1
        recipient.last_attempt_at = current_time
        conversation = _conversation_for_recipient(
            db, campaign=campaign, recipient=recipient, now=current_time
        )
        result = team_inbox_outbound.send_inbox_reply(
            db,
            conversation=conversation,
            payload=_payload_for_recipient(
                db,
                campaign,
                recipient,
                step=steps.get(recipient.step_id) if recipient.step_id else None,
                sender_override=sender_override,
            ),
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

    # Sessions run with autoflush=False, so the per-recipient status changes
    # above are still pending in the identity map. Flush before the aggregate
    # queries below, otherwise the counters and the completion check both read
    # the pre-send state and the campaign never leaves `sending`.
    db.flush()

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
        suppressed=suppressed,
    )


def mark_recipient_delivered(
    db: Session,
    recipient_id: str | UUID,
    *,
    now: datetime | None = None,
) -> CampaignRecipient:
    """Record a downstream delivery confirmation for a recipient."""
    recipient = db.get(CampaignRecipient, coerce_uuid(recipient_id))
    if recipient is None:
        raise HTTPException(status_code=404, detail="Campaign recipient not found")
    if recipient.status != CampaignRecipientStatus.sent.value:
        raise HTTPException(
            status_code=409, detail="Only sent recipients can be marked delivered"
        )
    recipient.status = CampaignRecipientStatus.delivered.value
    recipient.delivered_at = now or _now()
    db.flush()
    campaign = db.get(Campaign, recipient.campaign_id)
    if campaign is not None:
        _refresh_counts(db, campaign)
    db.flush()
    return recipient


def _refresh_counts(db: Session, campaign: Campaign) -> None:
    counts = Counter(
        status
        for (status,) in db.query(CampaignRecipient.status)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .all()
    )
    campaign.total_recipients = sum(counts.values()) - sum(
        counts.get(status, 0) for status in TERMINAL_RECIPIENT_STATUSES
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
    built = sent = failed = deferred = 0
    for campaign in campaigns:
        # Outside its send window the campaign simply stays `scheduled` and is
        # picked up by a later beat.
        if not within_send_window(campaign, current_time):
            deferred += 1
            continue
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
    return {
        "campaigns": len(campaigns),
        "built": built,
        "sent": sent,
        "failed": failed,
        "deferred": deferred,
    }


# ---------------------------------------------------------------------------
# Step sequencing
# ---------------------------------------------------------------------------


def _step_due_at(campaign: Campaign, steps: list[CampaignStep], index: int) -> datetime:
    """Absolute due time of `steps[index]`.

    `delay_days`/`delay_hours` are relative to the *previous stage*, so the
    offset from `sending_started_at` is the running sum up to and including
    this step. That keeps a sequence editable without re-basing every later step.
    """
    assert campaign.sending_started_at is not None
    offset = timedelta()
    for step in steps[: index + 1]:
        offset += timedelta(days=step.delay_days, hours=step.delay_hours)
    started = campaign.sending_started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return started + offset


def materialize_step_recipients(
    db: Session,
    campaign: Campaign,
    step: CampaignStep,
    *,
    now: datetime | None = None,
) -> CampaignStepMaterializeResult:
    """Create the recipient rows for one step of a sequence.

    A step targets exactly the people the initial send actually reached — rows
    that failed, were skipped, or were suppressed do not roll forward.
    """
    current_time = now or _now()
    seeds = (
        db.query(CampaignRecipient)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .filter(CampaignRecipient.step_id.is_(None))
        .filter(
            CampaignRecipient.status.in_(
                [
                    CampaignRecipientStatus.sent.value,
                    CampaignRecipientStatus.delivered.value,
                    CampaignRecipientStatus.opened.value,
                    CampaignRecipientStatus.clicked.value,
                ]
            )
        )
        .all()
    )
    existing = {
        subscriber_id
        for (subscriber_id,) in db.query(CampaignRecipient.subscriber_id)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .filter(CampaignRecipient.step_id == step.id)
        .all()
    }
    # Consent is asked of the platform ledger, not a campaign-local table.
    # `filter_eligible` is the SAME rule the delivery gate applies -- a
    # per-recipient loop here would be a second implementation that drifts from
    # it, and the drift would show up as mail to someone who unsubscribed.
    eligible = set(
        communication_eligibility.filter_eligible(
            db,
            channel=campaign.channel,
            addresses=[seed.address for seed in seeds],
            category=MARKETING_CATEGORY,
        )
    )
    blocked = {seed.address for seed in seeds if seed.address not in eligible}

    created = suppressed = 0
    for seed in seeds:
        if seed.subscriber_id in existing:
            continue
        if seed.address in blocked:
            suppressed += 1
            continue
        db.add(
            CampaignRecipient(
                campaign_id=campaign.id,
                subscriber_id=seed.subscriber_id,
                step_id=step.id,
                address=seed.address,
                email=seed.email,
                status=CampaignRecipientStatus.pending.value,
                unsubscribe_token=_new_unsubscribe_token(),
                metadata_={
                    "source": "native_campaign_step",
                    "campaign_step_index": step.step_index,
                },
            )
        )
        created += 1
    db.flush()

    if created:
        metadata = dict(campaign.metadata_ or {})
        materialized = dict(metadata.get("materialized_steps") or {})
        materialized[str(step.id)] = current_time.isoformat()
        metadata["materialized_steps"] = materialized
        campaign.metadata_ = metadata
        db.flush()

    return CampaignStepMaterializeResult(
        campaign_id=campaign.id,
        step_id=step.id,
        created=created,
        suppressed=suppressed,
    )


def _step_is_materialized(db: Session, campaign: Campaign, step: CampaignStep) -> bool:
    return (
        db.query(CampaignRecipient.id)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .filter(CampaignRecipient.step_id == step.id)
        .first()
        is not None
    )


def process_due_campaign_steps(
    db: Session, *, now: datetime | None = None, limit: int = 20
) -> dict[str, int]:
    """Advance nurture sequences: materialize and send any step that is due.

    Steps are strictly ordered — step N is never materialized before step N-1
    has been, even if its own due time has passed (e.g. after a backlog).
    """
    current_time = now or _now()
    campaigns = (
        db.query(Campaign)
        .filter(Campaign.is_active.is_(True))
        .filter(Campaign.campaign_type == CampaignType.nurture.value)
        .filter(
            Campaign.status.in_(
                [CampaignStatus.sending.value, CampaignStatus.completed.value]
            )
        )
        .filter(Campaign.sending_started_at.is_not(None))
        .order_by(Campaign.sending_started_at.asc())
        .limit(limit)
        .all()
    )

    advanced = created = sent = deferred = 0
    for campaign in campaigns:
        if not within_send_window(campaign, current_time):
            deferred += 1
            continue
        steps = [
            step for step in list_campaign_steps(db, campaign.id) if step.is_active
        ]
        for index, step in enumerate(steps):
            if _step_is_materialized(db, campaign, step):
                continue
            if current_time < _step_due_at(campaign, steps, index):
                break
            result = materialize_step_recipients(db, campaign, step, now=current_time)
            if result.created == 0:
                # Nothing to send for this step; still let the sequence advance.
                continue
            created += result.created
            advanced += 1
            campaign.status = CampaignStatus.sending.value
            campaign.completed_at = None
            send_result = send_campaign_batch(db, campaign.id, now=current_time)
            sent += send_result.sent
            # One step per beat keeps a long backlog from firing a whole
            # sequence into a subscriber's inbox at once.
            break

    return {
        "campaigns": len(campaigns),
        "advanced": advanced,
        "created": created,
        "sent": sent,
        "deferred": deferred,
    }


# Commit-owning entry points. The API layer must not own transaction boundaries
# (see the SOT service-ownership contract); these wrap the flush-only builders
# above so the campaign service decides when its work is durable.
def create_campaign_committed(
    db: Session, payload, *, created_by_system_user_id: str | UUID | None = None
) -> Campaign:
    campaign = create_campaign(
        db, payload, created_by_system_user_id=created_by_system_user_id
    )
    db.commit()
    db.refresh(campaign)
    return campaign


def update_campaign_committed(
    db: Session, campaign_id: str | UUID, payload
) -> Campaign:
    campaign = update_campaign(db, campaign_id, payload)
    db.commit()
    db.refresh(campaign)
    return campaign


def build_recipient_list_committed(
    db: Session, campaign_id: str | UUID, *, limit: int | None = None
) -> CampaignAudienceBuildResult:
    result = build_recipient_list(db, campaign_id, limit=limit)
    db.commit()
    return result


def send_campaign_batch_committed(
    db: Session,
    campaign_id: str | UUID,
    *,
    batch_size: int = 100,
    now: datetime | None = None,
) -> CampaignSendResult:
    result = send_campaign_batch(db, campaign_id, batch_size=batch_size, now=now)
    db.commit()
    return result


def create_campaign_step_committed(
    db: Session, campaign_id: str | UUID, payload
) -> CampaignStep:
    step = create_campaign_step(db, campaign_id, payload)
    db.commit()
    db.refresh(step)
    return step


def update_campaign_step_committed(
    db: Session, campaign_id: str | UUID, step_id: str | UUID, payload
) -> CampaignStep:
    step = update_campaign_step(db, campaign_id, step_id, payload)
    db.commit()
    db.refresh(step)
    return step


def delete_campaign_step_committed(
    db: Session, campaign_id: str | UUID, step_id: str | UUID
) -> None:
    delete_campaign_step(db, campaign_id, step_id)
    db.commit()


def create_sender_committed(db: Session, payload) -> CampaignSender:
    sender = create_sender(db, payload)
    db.commit()
    db.refresh(sender)
    return sender


def update_sender_committed(
    db: Session, sender_id: str | UUID, payload
) -> CampaignSender:
    sender = update_sender(db, sender_id, payload)
    db.commit()
    db.refresh(sender)
    return sender


def create_smtp_config_committed(db: Session, payload) -> CampaignSmtpConfig:
    config = create_smtp_config(db, payload)
    db.commit()
    db.refresh(config)
    return config


def update_smtp_config_committed(
    db: Session, smtp_config_id: str | UUID, payload
) -> CampaignSmtpConfig:
    config = update_smtp_config(db, smtp_config_id, payload)
    db.commit()
    db.refresh(config)
    return config






def unsubscribe_by_token_committed(
    db: Session, token: str, *, source: str = "unsubscribe_link"
) -> CommunicationSuppression:
    suppression = unsubscribe_by_token(db, token, source=source)
    db.commit()
    db.refresh(suppression)
    return suppression


def mark_recipient_delivered_committed(
    db: Session, recipient_id: str | UUID, *, now: datetime | None = None
) -> CampaignRecipient:
    recipient = mark_recipient_delivered(db, recipient_id, now=now)
    db.commit()
    db.refresh(recipient)
    return recipient


def process_due_campaign_steps_committed(
    db: Session, *, now: datetime | None = None, limit: int = 20
) -> dict[str, int]:
    result = process_due_campaign_steps(db, now=now, limit=limit)
    db.commit()
    return result
