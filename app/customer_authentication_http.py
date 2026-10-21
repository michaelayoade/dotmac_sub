"""HTTP evidence and error mapping for the shared customer password policy."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, Request

from app.services import customer_login_identity
from app.services import password_authentication as pa
from app.services.customer_authentication import (
    CustomerLoginError,
    CustomerLoginEvidence,
    CustomerLoginFailure,
)


def customer_login_evidence(
    request: Request, surface: pa.DenialSurface
) -> CustomerLoginEvidence:
    normalized = pa.denial_for_request(
        reason=pa.DenialReason.INVALID_CREDENTIALS,
        surface=surface,
        phase=pa.DenialPhase.PASSWORD,
        target=None,
        request=request,
    )
    correlation_id = normalized.correlation_id
    value = request.headers.get("x-request-id") or getattr(
        request.state, "request_id", None
    )
    if value:
        try:
            correlation_id = UUID(str(value))
        except (ValueError, TypeError):
            pass
    native = surface is pa.DenialSurface.CUSTOMER_API
    return CustomerLoginEvidence(
        correlation_id=correlation_id,
        trusted_ip=normalized.trusted_ip,
        surface=surface,
        eligibility_denial_status=403 if native else 401,
        reset_denial_status=428 if native else 401,
    )


def customer_login_http_error(
    error: CustomerLoginError
    | customer_login_identity.CustomerLoginIdentityError
    | pa.PasswordAuthenticationError,
) -> HTTPException:
    if isinstance(error, CustomerLoginError):
        status = {
            CustomerLoginFailure.INVALID_CREDENTIALS: 401,
            CustomerLoginFailure.LOCKED: 403,
            CustomerLoginFailure.RESET_REQUIRED: 428,
            CustomerLoginFailure.DISABLED: 403,
            CustomerLoginFailure.NOT_CUSTOMER: 403,
        }[error.kind]
    elif isinstance(error, customer_login_identity.CustomerLoginIdentityError):
        status = (
            409 if error.code == customer_login_identity.AMBIGUOUS_EMAIL_CODE else 403
        )
    else:
        status = {
            "locked": 403,
            "account_disabled": 403,
            "customer_required": 403,
            "must_change_password": 428,
            "credential_changed": 409,
            "lock_timeout": 503,
        }.get(error.kind, 401)
    return HTTPException(
        status_code=status,
        detail={"code": error.code, "message": error.message},
        headers={"Retry-After": "5"} if status == 503 else None,
    )
