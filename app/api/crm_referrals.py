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

from typing import Never
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
from app.services import referrals as referral_program
from app.services.auth_dependencies import require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.referrals import referrals as referrals_service

router = APIRouter(prefix="/crm", tags=["crm-referrals"])

# Public, no-auth capture/signup router (mounted separately in main.py).
public_router = APIRouter(prefix="/referrals", tags=["crm-referrals-public"])


def _conversion_source(auth: dict, surface: str) -> str:
    actor = str(auth.get("principal_id") or "unknown").strip()
    principal_type = str(auth.get("principal_type") or "user").strip()
    return f"{surface}:{principal_type}:{actor}"[:80]


def _conversion_error(
    exc: referral_account_conversion.ReferralAccountConversionError,
) -> Never:
    status_code = {
        "referrals.account_conversion.invalid_capability": status.HTTP_401_UNAUTHORIZED,
        "referrals.account_conversion.context_not_found": status.HTTP_404_NOT_FOUND,
        "referrals.account_conversion.subscriber_not_found": status.HTTP_404_NOT_FOUND,
        "referrals.account_conversion.invalid_command": (
            status.HTTP_422_UNPROCESSABLE_ENTITY
        ),
        "referrals.account_conversion.incomplete_context": status.HTTP_409_CONFLICT,
        "referrals.account_conversion.stale_context": status.HTTP_409_CONFLICT,
        "referrals.account_conversion.context_not_convertible": (
            status.HTTP_409_CONFLICT
        ),
        "referrals.account_conversion.account_conflict": status.HTTP_409_CONFLICT,
        "referrals.account_conversion.self_referral": status.HTTP_409_CONFLICT,
    }.get(exc.code, status.HTTP_500_INTERNAL_SERVER_ERROR)
    raise HTTPException(status_code=status_code, detail=exc.message) from exc


def _program_error(exc: DomainError) -> Never:
    suffix = exc.code.removeprefix("referrals.program.")
    status_code = {
        "subscriber_not_found": status.HTTP_404_NOT_FOUND,
        "referral_not_found": status.HTTP_404_NOT_FOUND,
        "code_not_found": status.HTTP_404_NOT_FOUND,
        "invalid_command": status.HTTP_422_UNPROCESSABLE_ENTITY,
        "contact_required": status.HTTP_422_UNPROCESSABLE_ENTITY,
        "invalid_filter": status.HTTP_400_BAD_REQUEST,
        "program_disabled": status.HTTP_503_SERVICE_UNAVAILABLE,
        "invalid_configuration": status.HTTP_503_SERVICE_UNAVAILABLE,
        "self_referral": status.HTTP_409_CONFLICT,
        "existing_customer": status.HTTP_409_CONFLICT,
        "invalid_transition": status.HTTP_409_CONFLICT,
        "account_attachment_required": status.HTTP_409_CONFLICT,
        "idempotency_conflict": status.HTTP_409_CONFLICT,
        "financial_conflict": status.HTTP_409_CONFLICT,
        "incomplete_reward_evidence": status.HTTP_409_CONFLICT,
        "write_conflict": status.HTTP_409_CONFLICT,
    }.get(suffix, status.HTTP_500_INTERNAL_SERVER_ERROR)
    detail = (
        exc.message
        if exc.code.startswith("referrals.program.")
        else "Referral command failed."
    )
    raise HTTPException(status_code=status_code, detail=detail) from exc


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
    try:
        items = referrals_service.list(
            db,
            status=status,
            referrer_subscriber_id=referrer_subscriber_id,
            limit=limit,
            offset=offset,
        )
    except DomainError as exc:
        _program_error(exc)
    return {"items": items, "count": len(items), "limit": limit, "offset": offset}


@router.get(
    "/referrals/{referral_id}",
    response_model=ReferralRead,
    dependencies=[Depends(require_permission("crm:lead:read"))],
)
def get_referral(referral_id: str, db: Session = Depends(get_db)):
    try:
        return referrals_service.get(db, referral_id)
    except DomainError as exc:
        _program_error(exc)


@router.post(
    "/referrals/{referral_id}/issue-reward",
    response_model=ReferralRead,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def issue_referral_reward(
    referral_id: str,
    db: Session = Depends(get_db),
    auth: dict = Depends(get_current_user),
):
    """Issue the referrer's reward as an account credit (idempotent on
    ``external_ref="referral:{id}"``) and mark the referral rewarded."""
    try:
        resolved_referral_id = UUID(referral_id)
        db_session_adapter.release_read_transaction(db)
        referral_program.issue_referral_reward(
            db,
            referral_program.IssueReferralRewardCommand(
                context=CommandContext.system(
                    actor=_conversion_source(auth, "staff_referral_reward"),
                    scope=referral_program.REFERRAL_PROGRAM_SCOPE,
                    reason="Staff requested referral reward issuance",
                    idempotency_key=f"referral-reward:{resolved_referral_id}",
                ),
                referral_id=resolved_referral_id,
            ),
        )
        return referrals_service.get(db, resolved_referral_id)
    except (ValueError, DomainError) as exc:
        if isinstance(exc, DomainError):
            _program_error(exc)
        raise HTTPException(status_code=404, detail="Referral not found.") from exc


@router.post(
    "/referrals/{referral_id}/reject",
    response_model=ReferralRead,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def reject_referral(
    referral_id: str,
    payload: ReferralRejectRequest,
    db: Session = Depends(get_db),
    auth: dict = Depends(get_current_user),
):
    try:
        resolved_referral_id = UUID(referral_id)
        db_session_adapter.release_read_transaction(db)
        referral_program.reject_referral(
            db,
            referral_program.RejectReferralCommand(
                context=CommandContext.system(
                    actor=_conversion_source(auth, "staff_referral_reject"),
                    scope=referral_program.REFERRAL_PROGRAM_SCOPE,
                    reason="Staff rejected the referral",
                    idempotency_key=f"referral-reject:{resolved_referral_id}",
                ),
                referral_id=resolved_referral_id,
                reason=payload.reason,
            ),
        )
        return referrals_service.get(db, resolved_referral_id)
    except (ValueError, DomainError) as exc:
        if isinstance(exc, DomainError):
            _program_error(exc)
        raise HTTPException(status_code=404, detail="Referral not found.") from exc


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
        resolved_referral_id = UUID(referral_id)
        db_session_adapter.release_read_transaction(db)
        return referral_account_conversion.attach_existing_account(
            db,
            referral_account_conversion.AttachExistingReferralAccountCommand(
                context=CommandContext.system(
                    actor=_conversion_source(auth, "staff_referral_attach"),
                    scope=(
                        referral_account_conversion.REFERRAL_ACCOUNT_CONVERSION_SCOPE
                    ),
                    reason=payload.reason,
                    idempotency_key=(
                        f"referral-account-attach:{resolved_referral_id}:"
                        f"{payload.subscriber_id}"
                    ),
                ),
                referral_id=resolved_referral_id,
                referred_party_id=payload.referred_party_id,
                referred_lead_id=payload.referred_lead_id,
                subscriber_id=payload.subscriber_id,
            ),
        )
    except referral_account_conversion.ReferralAccountConversionError as exc:
        _conversion_error(exc)
    except ValueError as exc:
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
        resolved_referral_id = UUID(referral_id)
        db_session_adapter.release_read_transaction(db)
        return referral_account_conversion.create_account(
            db,
            referral_account_conversion.CreateReferralAccountCommand(
                context=CommandContext.system(
                    actor=_conversion_source(auth, "staff_referral_create"),
                    scope=(
                        referral_account_conversion.REFERRAL_ACCOUNT_CONVERSION_SCOPE
                    ),
                    reason=payload.reason,
                    idempotency_key=(f"referral-account-create:{resolved_referral_id}"),
                ),
                referral_id=resolved_referral_id,
                referred_party_id=payload.referred_party_id,
                referred_lead_id=payload.referred_lead_id,
                subscriber_payload=payload.subscriber,
            ),
        )
    except referral_account_conversion.ReferralAccountConversionError as exc:
        _conversion_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Referral not found") from exc


@router.post(
    "/subscribers/{subscriber_id}/referral-code",
    response_model=ReferralCodeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("crm:lead:write"))],
)
def ensure_referral_code(
    subscriber_id: str,
    db: Session = Depends(get_db),
    auth: dict = Depends(get_current_user),
):
    """Get or mint the active referral code for a referrer (a subscriber)."""
    try:
        resolved_subscriber_id = UUID(subscriber_id)
        db_session_adapter.release_read_transaction(db)
        result = referral_program.ensure_referral_code(
            db,
            referral_program.EnsureReferralCodeCommand(
                context=CommandContext.system(
                    actor=_conversion_source(auth, "staff_referral_code"),
                    scope=referral_program.REFERRAL_PROGRAM_SCOPE,
                    reason="Staff requested the Subscriber referral code",
                    idempotency_key=f"referral-code:{resolved_subscriber_id}",
                ),
                subscriber_id=resolved_subscriber_id,
            ),
        )
        return referrals_service.get_code(db, result.referral_code_id)
    except (ValueError, DomainError) as exc:
        if isinstance(exc, DomainError):
            _program_error(exc)
        raise HTTPException(status_code=404, detail="Subscriber not found.") from exc


@public_router.post(
    "/capture",
    response_model=ReferralCaptureRead,
    status_code=status.HTTP_201_CREATED,
)
def capture_referral(payload: ReferralCaptureRequest, db: Session = Depends(get_db)):
    """Capture the prospect and return a signed continuation into signup."""

    try:
        db_session_adapter.release_read_transaction(db)
        result = referral_program.capture_referral(
            db,
            referral_program.CaptureReferralCommand(
                context=CommandContext.system(
                    actor="public_referral_capture",
                    scope=referral_program.REFERRAL_PROGRAM_SCOPE,
                    reason="Public prospect submitted a referral capture",
                ),
                code=payload.code,
                name=payload.name,
                email=payload.email,
                phone=payload.phone,
                region=payload.region,
                address=payload.address,
                notes=payload.notes,
                source="public",
            ),
        )
        referral = referrals_service.get(db, result.referral_id)
        signed = referral_account_conversion.issue_public_signup_context(db, referral)
    except referral_program.ReferralProgramError as exc:
        _program_error(exc)
    except referral_account_conversion.ReferralAccountConversionError as exc:
        _conversion_error(exc)
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
        db_session_adapter.release_read_transaction(db)
        account = referral_account_conversion.create_public_account(
            db,
            referral_account_conversion.CreatePublicReferralAccountCommand(
                context=CommandContext.system(
                    actor="public_referral_signup",
                    scope=(
                        referral_account_conversion.REFERRAL_ACCOUNT_CONVERSION_SCOPE
                    ),
                    reason=(
                        "Public signup presented the signed Referral, Party, and "
                        "Lead context"
                    ),
                ),
                conversion_token=payload.conversion_token,
                account_payload=payload.account,
            ),
        )
        enrollment = customer_credential_enrollment.request_referral_enrollment(
            db,
            customer_credential_enrollment.RequestReferralEnrollmentCommand(
                context=CommandContext.system(
                    actor="service:public-referral-signup",
                    scope=customer_credential_enrollment.CUSTOMER_CREDENTIAL_ENROLLMENT_SCOPE,
                    reason="Request referral customer credential enrollment",
                    idempotency_key=(
                        f"referral-credential-enrollment:{account.referral_id}"
                    ),
                ),
                referral_id=account.referral_id,
                referred_party_id=account.referred_party_id,
                referred_lead_id=account.referred_lead_id,
                subscriber_id=account.subscriber_id,
            ),
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
    except DomainError as exc:
        status_code = {
            "auth.customer_credential_enrollment.context_not_found": (
                status.HTTP_404_NOT_FOUND
            ),
            "auth.customer_credential_enrollment.stale_context": (
                status.HTTP_409_CONFLICT
            ),
            "auth.customer_credential_enrollment.inactive_account": (
                status.HTTP_409_CONFLICT
            ),
        }.get(exc.code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        raise HTTPException(status_code=status_code, detail=exc.message) from exc
