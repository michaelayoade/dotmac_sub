from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.notification import SuppressionReason, SuppressionScope
from app.schemas.campaigns import (
    CampaignAudienceBuildRead,
    CampaignCreate,
    CampaignRead,
    CampaignRecipientRead,
    CampaignSenderCreate,
    CampaignSenderRead,
    CampaignSenderUpdate,
    CampaignSendRead,
    CampaignStepCreate,
    CampaignStepRead,
    CampaignStepUpdate,
    CampaignUnsubscribeRead,
    CampaignUpdate,
    SuppressionCreate,
)
from app.schemas.common import ListResponse
from app.schemas.notification import CommunicationSuppressionRead
from app.services import comms_campaigns, communication_eligibility
from app.services.auth_dependencies import require_user_auth
from app.services.response import list_response

router = APIRouter(prefix="/campaigns", tags=["campaigns"])
# Unsubscribe must work from an email client with no session. Mounted without
# auth in app.main (dependency mode "none").
public_router = APIRouter(prefix="/campaigns/public", tags=["campaigns"])


def _actor_id(auth: dict) -> str | None:
    if auth.get("principal_type") == "system_user":
        return str(auth.get("principal_id"))
    return None


def _campaign_read(campaign) -> CampaignRead:
    return CampaignRead(
        id=campaign.id,
        crm_campaign_id=campaign.crm_campaign_id,
        name=campaign.name,
        campaign_type=campaign.campaign_type,
        channel=campaign.channel,
        status=campaign.status,
        subject=campaign.subject,
        scheduled_at=campaign.scheduled_at,
        send_window_start_hour=campaign.send_window_start_hour,
        send_window_end_hour=campaign.send_window_end_hour,
        send_window_timezone=campaign.send_window_timezone,
        sending_started_at=campaign.sending_started_at,
        completed_at=campaign.completed_at,
        total_recipients=campaign.total_recipients,
        sent_count=campaign.sent_count,
        delivered_count=campaign.delivered_count,
        failed_count=campaign.failed_count,
        opened_count=campaign.opened_count,
        clicked_count=campaign.clicked_count,
        campaign_sender_id=campaign.campaign_sender_id,
        service_team_id=campaign.service_team_id,
        metadata=campaign.metadata_,
        created_at=campaign.created_at,
        updated_at=campaign.updated_at,
    )


def _step_read(step) -> CampaignStepRead:
    return CampaignStepRead(
        id=step.id,
        campaign_id=step.campaign_id,
        step_index=step.step_index,
        name=step.name,
        subject=step.subject,
        body_html=step.body_html,
        body_text=step.body_text,
        delay_days=step.delay_days,
        delay_hours=step.delay_hours,
        is_active=step.is_active,
        created_at=step.created_at,
        updated_at=step.updated_at,
    )


def _sender_read(sender) -> CampaignSenderRead:
    return CampaignSenderRead(
        id=sender.id,
        name=sender.name,
        sender_key=sender.sender_key,
        is_active=sender.is_active,
        metadata=sender.metadata_,
        created_at=sender.created_at,
        updated_at=sender.updated_at,
    )


@router.post("", response_model=CampaignRead, status_code=status.HTTP_201_CREATED)
def create_campaign(
    payload: CampaignCreate,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    campaign = comms_campaigns.create_campaign_committed(
        db,
        payload,
        created_by_system_user_id=_actor_id(auth),
    )
    return _campaign_read(campaign)


@router.get("", response_model=ListResponse[CampaignRead])
def list_campaigns(
    status: str | None = None,
    channel: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = [
        _campaign_read(campaign)
        for campaign in comms_campaigns.list_campaigns(
            db,
            status=status,
            channel=channel,
            limit=limit,
            offset=offset,
        )
    ]
    return list_response(items, limit, offset)


@router.patch("/{campaign_id}", response_model=CampaignRead)
def update_campaign(
    campaign_id: UUID,
    payload: CampaignUpdate,
    db: Session = Depends(get_db),
):
    campaign = comms_campaigns.update_campaign_committed(db, campaign_id, payload)
    return _campaign_read(campaign)


@router.post("/{campaign_id}/audience", response_model=CampaignAudienceBuildRead)
def build_campaign_audience(
    campaign_id: UUID,
    limit: int | None = Query(default=None, ge=1, le=10000),
    db: Session = Depends(get_db),
):
    result = comms_campaigns.build_recipient_list_committed(
        db, campaign_id, limit=limit
    )
    return CampaignAudienceBuildRead(**result.__dict__)


@router.post("/{campaign_id}/send", response_model=CampaignSendRead)
def send_campaign_batch(
    campaign_id: UUID,
    batch_size: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    result = comms_campaigns.send_campaign_batch_committed(
        db,
        campaign_id,
        batch_size=batch_size,
    )
    return CampaignSendRead(**result.__dict__)


@router.get(
    "/{campaign_id}/recipients", response_model=ListResponse[CampaignRecipientRead]
)
def list_campaign_recipients(
    campaign_id: UUID,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    rows = comms_campaigns.list_campaign_recipients(
        db,
        campaign_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    items = [
        CampaignRecipientRead(
            id=row.id,
            campaign_id=row.campaign_id,
            subscriber_id=row.subscriber_id,
            step_id=row.step_id,
            address=row.address,
            email=row.email,
            status=row.status,
            conversation_id=row.conversation_id,
            message_id=row.message_id,
            sent_at=row.sent_at,
            delivered_at=row.delivered_at,
            suppressed_at=row.suppressed_at,
            attempt_count=row.attempt_count,
            last_attempt_at=row.last_attempt_at,
            failed_reason=row.failed_reason,
            metadata=row.metadata_,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return list_response(items, limit, offset)


# --- Steps ------------------------------------------------------------------


@router.get("/{campaign_id}/steps", response_model=list[CampaignStepRead])
def list_campaign_steps(
    campaign_id: UUID,
    db: Session = Depends(get_db),
):
    return [
        _step_read(step)
        for step in comms_campaigns.list_campaign_steps(db, campaign_id)
    ]


@router.post(
    "/{campaign_id}/steps",
    response_model=CampaignStepRead,
    status_code=status.HTTP_201_CREATED,
)
def create_campaign_step(
    campaign_id: UUID,
    payload: CampaignStepCreate,
    db: Session = Depends(get_db),
):
    step = comms_campaigns.create_campaign_step_committed(db, campaign_id, payload)
    return _step_read(step)


@router.patch("/{campaign_id}/steps/{step_id}", response_model=CampaignStepRead)
def update_campaign_step(
    campaign_id: UUID,
    step_id: UUID,
    payload: CampaignStepUpdate,
    db: Session = Depends(get_db),
):
    step = comms_campaigns.update_campaign_step_committed(
        db, campaign_id, step_id, payload
    )
    return _step_read(step)


@router.delete("/{campaign_id}/steps/{step_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_campaign_step(
    campaign_id: UUID,
    step_id: UUID,
    db: Session = Depends(get_db),
):
    comms_campaigns.delete_campaign_step_committed(db, campaign_id, step_id)


# --- Sender profiles --------------------------------------------------------


@router.get("/senders", response_model=ListResponse[CampaignSenderRead])
def list_campaign_senders(
    is_active: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = [
        _sender_read(sender)
        for sender in comms_campaigns.list_senders(
            db, is_active=is_active, limit=limit, offset=offset
        )
    ]
    return list_response(items, limit, offset)


@router.post(
    "/senders", response_model=CampaignSenderRead, status_code=status.HTTP_201_CREATED
)
def create_campaign_sender(
    payload: CampaignSenderCreate,
    db: Session = Depends(get_db),
):
    return _sender_read(comms_campaigns.create_sender_committed(db, payload))


@router.patch("/senders/{sender_id}", response_model=CampaignSenderRead)
def update_campaign_sender(
    sender_id: UUID,
    payload: CampaignSenderUpdate,
    db: Session = Depends(get_db),
):
    return _sender_read(comms_campaigns.update_sender_committed(db, sender_id, payload))


# --- Suppression ------------------------------------------------------------


@router.get("/suppressions", response_model=ListResponse[CommunicationSuppressionRead])
def list_campaign_suppressions(
    channel: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = communication_eligibility.list_suppressions(
        db, channel=channel, limit=limit, offset=offset
    )
    return list_response(items, limit, offset)


@router.post(
    "/suppressions",
    response_model=CommunicationSuppressionRead,
    status_code=status.HTTP_201_CREATED,
)
def create_campaign_suppression(
    payload: SuppressionCreate,
    auth=Depends(require_user_auth),
    db: Session = Depends(get_db),
):
    # Scope is pinned to `marketing`. An operator suppressing someone from the
    # campaign screen is recording a marketing refusal -- it is not authority to
    # stop sending that customer their invoice. `all` is for hard bounces and
    # erasure requests, and this is not the endpoint that sets it.
    suppression = communication_eligibility.suppress_committed(
        db,
        channel=payload.channel,
        address=payload.address,
        scope=SuppressionScope.marketing,
        reason=SuppressionReason.manual,
        subscriber_id=payload.subscriber_id,
        note=payload.note,
        created_by=_actor_id(auth) or "admin",
    )
    return suppression


@router.delete("/suppressions", status_code=status.HTTP_204_NO_CONTENT)
def delete_campaign_suppression(
    channel: str = Query(...),
    address: str = Query(...),
    db: Session = Depends(get_db),
):
    communication_eligibility.unsuppress_marketing_committed(
        db, channel=channel, address=address
    )


def _unsubscribe(token: str, db: Session) -> CampaignUnsubscribeRead:
    suppression = comms_campaigns.unsubscribe_by_token_committed(db, token)
    return CampaignUnsubscribeRead(
        unsubscribed=True,
        channel=suppression.channel.value,
        address=suppression.address,
    )


@public_router.get(
    "/unsubscribe/{token}",
    response_model=CampaignUnsubscribeRead,
    operation_id="campaign_unsubscribe_get",
)
def unsubscribe_get(
    token: str,
    db: Session = Depends(get_db),
) -> CampaignUnsubscribeRead:
    return _unsubscribe(token, db)


@public_router.post(
    "/unsubscribe/{token}",
    response_model=CampaignUnsubscribeRead,
    operation_id="campaign_unsubscribe_post",
)
def unsubscribe_post(
    token: str,
    db: Session = Depends(get_db),
) -> CampaignUnsubscribeRead:
    return _unsubscribe(token, db)
