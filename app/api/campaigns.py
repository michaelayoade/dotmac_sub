from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.campaigns import (
    CampaignAudienceBuildRead,
    CampaignCreate,
    CampaignRead,
    CampaignRecipientRead,
    CampaignSendRead,
    CampaignUpdate,
)
from app.schemas.common import ListResponse
from app.services import comms_campaigns
from app.services.auth_dependencies import require_user_auth
from app.services.response import list_response

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


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
        sending_started_at=campaign.sending_started_at,
        completed_at=campaign.completed_at,
        total_recipients=campaign.total_recipients,
        sent_count=campaign.sent_count,
        delivered_count=campaign.delivered_count,
        failed_count=campaign.failed_count,
        opened_count=campaign.opened_count,
        clicked_count=campaign.clicked_count,
        service_team_id=campaign.service_team_id,
        metadata=campaign.metadata_,
        created_at=campaign.created_at,
        updated_at=campaign.updated_at,
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
            failed_reason=row.failed_reason,
            metadata=row.metadata_,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return list_response(items, limit, offset)
