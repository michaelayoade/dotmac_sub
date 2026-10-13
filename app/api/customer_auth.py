"""Customer-only JSON adapters using the customer portal's password policy."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.customer_authentication_http import (
    customer_login_evidence,
    customer_login_http_error,
)
from app.db import get_db
from app.schemas.auth_flow import (
    CustomerLoginRequest,
    LoginResponse,
    MfaVerifyRequest,
    TokenResponse,
)
from app.services import auth_flow, customer_authentication, customer_login_identity
from app.services import password_authentication as pa

router = APIRouter(prefix="/customer/auth", tags=["customer-auth"])


@router.post("/login", response_model=LoginResponse)
def login(
    payload: CustomerLoginRequest, request: Request, db: Session = Depends(get_db)
) -> LoginResponse:
    denial_target: pa.DenialTarget | None = None
    try:
        prepared = customer_authentication.prepare_customer_login(
            db=db,
            query=customer_authentication.PrepareCustomerLogin(
                identifier=payload.username,
                password=payload.password,
                evidence=customer_login_evidence(
                    request, pa.DenialSurface.CUSTOMER_API
                ),
                audience=customer_authentication.CustomerLoginAudience.NATIVE_CUSTOMER,
            ),
        )
        denial_target = prepared.denial_target
        outcome = pa.complete_password_step(
            db=db, verified=prepared.verified, request=request
        )
    except (
        customer_authentication.CustomerLoginError,
        customer_login_identity.CustomerLoginIdentityError,
    ) as error:
        raise customer_login_http_error(error) from error
    except pa.PasswordAuthenticationError as error:
        mapped = customer_login_http_error(error)
        pa.record_authentication_denial(
            pa.denial_for_request(
                reason=auth_flow._owner_denial_reason(error),
                surface=pa.DenialSurface.CUSTOMER_API,
                phase=pa.DenialPhase.OWNER,
                target=denial_target,
                request=request,
                status_code=auth_flow._owner_denial_status(error),
            )
        )
        raise mapped from error
    if outcome.kind == "mfa_challenge":
        return LoginResponse(mfa_required=True, mfa_token=outcome.challenge_token)
    if outcome.staged_session is None:
        raise RuntimeError("Customer session was not issued")
    return LoginResponse.model_validate(
        auth_flow.tokens_for_staged_session(outcome.staged_session)
    )


@router.post("/mfa/verify", response_model=TokenResponse)
def verify_mfa(
    payload: MfaVerifyRequest, request: Request, db: Session = Depends(get_db)
) -> TokenResponse:
    # The shared MFA owner checks the audience, source/version standing and
    # exact customer eligibility again before it can issue any session.
    result = auth_flow.AuthFlow.mfa_verify(
        db=db,
        mfa_token=payload.mfa_token,
        code=payload.code,
        request=request,
        audience=auth_flow.LoginAudience.customer,
        denial_surface=pa.DenialSurface.CUSTOMER_API,
    )
    return TokenResponse.model_validate(result)
