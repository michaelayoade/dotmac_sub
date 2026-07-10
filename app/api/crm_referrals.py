"""Referral program API: staff management + public capture (Phase 3 §2.4).

Ported from CRM ``app/api/crm/referrals.py``. Staff routes ride the existing
``crm:lead:*`` permissions by design (referrals are part of the sales/lead
funnel — already seeded in sub RBAC); dedicated ``crm:referral:*`` permissions
can be split out later. The capture endpoint is public (a prospect using a
shared ``/r/{code}`` referral link) and lives on a no-auth router mounted
separately in ``main.py``.

The referrer subject is a subscriber (§1.6), so the CRM's
``POST /crm/people/{person_id}/referral-code`` becomes
``POST /crm/subscribers/{subscriber_id}/referral-code``.
"""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.common import ListResponse
from app.schemas.referral import (
    ReferralCaptureRequest,
    ReferralCodeRead,
    ReferralRead,
    ReferralRejectRequest,
)
from app.services.auth_dependencies import require_permission
from app.services.referrals import referrals as referrals_service

router = APIRouter(prefix="/crm", tags=["crm-referrals"])

# Public, no-auth capture router (mounted separately in main.py).
public_router = APIRouter(prefix="/referrals", tags=["crm-referrals-public"])


@router.get(
    "/referrals",
    response_model=ListResponse[ReferralRead],
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def list_referrals(
    status: str | None = None,
    referrer_subscriber_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    items = referrals_service.list(
        db,
        status=status,
        referrer_subscriber_id=referrer_subscriber_id,
        limit=limit,
        offset=offset,
    )
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.get(
    "/referrals/{referral_id}",
    response_model=ReferralRead,
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def get_referral(referral_id: str, db: Session = Depends(get_db)):
    return referrals_service.get(db, referral_id)


@router.post(
    "/referrals/{referral_id}/issue-reward",
    response_model=ReferralRead,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def issue_referral_reward(referral_id: str, db: Session = Depends(get_db)):
    """Pay the referrer's reward into their wallet (idempotent on
    ``external_ref="referral:{id}"``) and mark the referral rewarded."""
    return referrals_service.issue_reward(db, referral_id)


@router.post(
    "/referrals/{referral_id}/reject",
    response_model=ReferralRead,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def reject_referral(
    referral_id: str, payload: ReferralRejectRequest, db: Session = Depends(get_db)
):
    return referrals_service.reject(db, referral_id, payload.reason)


@router.post(
    "/subscribers/{subscriber_id}/referral-code",
    response_model=ReferralCodeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def ensure_referral_code(subscriber_id: str, db: Session = Depends(get_db)):
    """Get or mint the active referral code for a referrer (a subscriber)."""
    return referrals_service.ensure_code(db, subscriber_id)


@public_router.post(
    "/capture",
    response_model=ReferralRead,
    status_code=status.HTTP_201_CREATED,
)
def capture_referral(payload: ReferralCaptureRequest, db: Session = Depends(get_db)):
    """Public: a prospect signs up via a referral code. Creates an attributed lead."""
    return referrals_service.capture(
        db,
        code=payload.code,
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        region=payload.region,
        address=payload.address,
        notes=payload.notes,
        source="public",
    )
