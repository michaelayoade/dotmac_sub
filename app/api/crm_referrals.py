"""Native referral API for staff management and public capture.

Ported from CRM ``app/api/crm/referrals.py``. Staff routes ride the existing
``crm:lead:*`` permissions by design (referrals are part of the sales/lead
funnel — already seeded in sub RBAC); dedicated ``crm:referral:*`` permissions
can be split out later. The capture endpoint is public (a prospect using a
shared ``/r/{code}`` referral link) and lives on a no-auth router mounted
separately in ``main.py``.

The referrer subject is a subscriber, so the CRM's
``POST /crm/people/{person_id}/referral-code`` becomes
``POST /crm/subscribers/{subscriber_id}/referral-code``.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.schemas.common import ListResponse
from app.schemas.referral import (
    ReferralAccountConversionRead,
    ReferralCaptureRead,
    ReferralCaptureRequest,
    ReferralCodeRead,
    ReferralRead,
    ReferralRejectRequest,
    ReferralSelfServiceSignupRead,
    ReferralSelfServiceSignupRequest,
    ReferralSubscriberAttachRequest,
    ReferralSubscriberCreateRequest,
)
from app.services import customer_credential_enrollment, referral_account_conversion
from app.services.auth_dependencies import require_permission
from app.services.referrals import referrals as referrals_service

router = APIRouter(prefix="/crm", tags=["crm-referrals"])

# Public, no-auth capture/signup router (mounted separately in main.py).
public_router = APIRouter(prefix="/referrals", tags=["crm-referrals-public"])


def _conversion_source(auth: dict, surface: str) -> str:
    actor = str(auth.get("principal_id") or "unknown").strip()
    principal_type = str(auth.get("principal_type") or "user").strip()
    return f"{surface}:{principal_type}:{actor}"[:80]


def _conversion_error(exc: referral_account_conversion.ReferralAccountConversionError):
    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


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
    """Issue the referrer's reward as an account credit (idempotent on
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
    "/referrals/{referral_id}/attach-subscriber",
    response_model=ReferralAccountConversionRead,
    dependencies=[
        Depends(require_permission("crm:lead:write")),
        Depends(require_permission("customer:update")),
    ],
)
def attach_referral_subscriber(
    referral_id: str,
    payload: ReferralSubscriberAttachRequest,
    db: Session = Depends(get_db),
    auth: dict = Depends(get_current_user),
):
    """Reviewed exact-Party adjudication for an existing Subscriber account."""

    try:
        return referral_account_conversion.attach_existing_account(
            db,
            referral_id=UUID(referral_id),
            referred_party_id=payload.referred_party_id,
            referred_lead_id=payload.referred_lead_id,
            subscriber_id=payload.subscriber_id,
            source=_conversion_source(auth, "staff_referral_attach"),
            reason=payload.reason,
        )
    except (
        ValueError,
        referral_account_conversion.ReferralAccountConversionError,
    ) as exc:
        if isinstance(exc, referral_account_conversion.ReferralAccountConversionError):
            _conversion_error(exc)
        raise HTTPException(status_code=404, detail="Referral not found") from exc


@router.post(
    "/referrals/{referral_id}/create-subscriber",
    response_model=ReferralAccountConversionRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(require_permission("crm:lead:write")),
        Depends(require_permission("customer:create")),
    ],
)
def create_referral_subscriber(
    referral_id: str,
    payload: ReferralSubscriberCreateRequest,
    db: Session = Depends(get_db),
    auth: dict = Depends(get_current_user),
):
    """Create an account without losing the exact Referral/Party/Lead context."""

    try:
        return referral_account_conversion.create_account(
            db,
            referral_id=UUID(referral_id),
            referred_party_id=payload.referred_party_id,
            referred_lead_id=payload.referred_lead_id,
            subscriber_payload=payload.subscriber,
            source=_conversion_source(auth, "staff_referral_create"),
            reason=payload.reason,
        )
    except (
        ValueError,
        referral_account_conversion.ReferralAccountConversionError,
    ) as exc:
        if isinstance(exc, referral_account_conversion.ReferralAccountConversionError):
            _conversion_error(exc)
        raise HTTPException(status_code=404, detail="Referral not found") from exc


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
    response_model=ReferralCaptureRead,
    status_code=status.HTTP_201_CREATED,
)
def capture_referral(payload: ReferralCaptureRequest, db: Session = Depends(get_db)):
    """Capture the prospect and return a signed continuation into signup."""

    referral = referrals_service.capture(
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
    signed = referral_account_conversion.issue_public_signup_context(db, referral)
    return ReferralCaptureRead(
        **ReferralRead.model_validate(referral).model_dump(),
        conversion_token=signed.token,
        conversion_expires_at=signed.expires_at,
    )


@public_router.post(
    "/signup",
    response_model=ReferralSelfServiceSignupRead,
    status_code=status.HTTP_201_CREATED,
)
def signup_referral_account(
    payload: ReferralSelfServiceSignupRequest,
    db: Session = Depends(get_db),
):
    """Create the exact account, then queue separate credential enrollment."""

    try:
        account = referral_account_conversion.create_public_account(
            db,
            conversion_token=payload.conversion_token,
            account_payload=payload.account,
        )
        enrollment = customer_credential_enrollment.request_referral_enrollment(
            db,
            referral_id=account.referral_id,
            referred_party_id=account.referred_party_id,
            referred_lead_id=account.referred_lead_id,
            subscriber_id=account.subscriber_id,
        )
        return ReferralSelfServiceSignupRead(
            referral_id=account.referral_id,
            referred_party_id=account.referred_party_id,
            referred_lead_id=account.referred_lead_id,
            subscriber_id=account.subscriber_id,
            outcome=account.outcome,
            enrollment_status=enrollment.status,
            enrollment_retry_after_seconds=enrollment.retry_after_seconds,
        )
    except referral_account_conversion.ReferralAccountConversionError as exc:
        _conversion_error(exc)
    except customer_credential_enrollment.CustomerCredentialEnrollmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
