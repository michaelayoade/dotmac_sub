"""HTTP adapter for the field-mobile OIDC federation seam.

Thin by construction: it validates the wire shape, builds a typed command with
keyword arguments, delegates to the ONE owner
(``app.services.oidc_mobile_federation``), and maps that owner's transport-
neutral errors onto status codes. It issues no query, makes no decision, and
holds no policy — the redirect comparison, the algorithm allowlist, the replay
refusal and the session issuance all live behind the service boundary.

Both routes are deliberately UNAUTHENTICATED, and that is not an exemption:
they are pre-authentication continuations, the same class as
``POST /auth/login``. Neither creates a party, and neither grants anything a
verified assertion plus an operator-installed binding has not already earned.
The gate that matters is the feature control, which the owner reads first and
which fails closed.

Every refusal maps to 401 with a safe category. A caller is never told whether
the subject exists, whether the binding is disabled, or whether the ceremony
was ever real — the categories are recorded for the operator in metrics, logs
and events, not handed to whoever can drive a login.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.oidc_mobile import (
    OidcMobileExchangeRequest,
    OidcMobileExchangeResponse,
    OidcMobileStartRequest,
    OidcMobileStartResponse,
    OidcRefusalResponse,
)
from app.services import oidc_mobile_federation as federation
from app.services.oidc_mobile_config import OidcFederationConfigError
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/auth/oidc/mobile", tags=["auth"])

_ACTOR = "public:field-mobile-oidc"


def _context(reason: str) -> CommandContext:
    return CommandContext.system(
        actor=_ACTOR,
        scope=federation.OIDC_MOBILE_FEDERATION_SCOPE,
        reason=reason,
    )


def _refusal(exc: federation.OidcFederationRefused) -> HTTPException:
    """One status code for every refusal.

    Varying the code by reason would restore exactly the oracle the single
    message removes: a 404 for "no such ceremony" against a 403 for "disabled
    binding" tells a prober which half of its guess was right.
    """

    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": exc.code, "reason": exc.reason},
    )


def _misconfigured(exc: OidcFederationConfigError) -> HTTPException:
    """503, not 500: the deployment is incomplete, the request was fine."""

    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": exc.code, "reason": "configuration_incomplete"},
    )


@router.post(
    "/start",
    response_model=OidcMobileStartResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": OidcRefusalResponse},
        503: {"model": OidcRefusalResponse},
    },
)
def start_ceremony(
    payload: OidcMobileStartRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> OidcMobileStartResponse:
    """Begin a ceremony. Creates no user and no session."""

    try:
        started = federation.start_mobile_ceremony(
            db,
            federation.StartMobileCeremonyCommand(
                context=_context("Field mobile OIDC ceremony start"),
                code_challenge_method=payload.code_challenge_method,
                device_id=request.headers.get("x-device-id"),
            ),
        )
    except federation.OidcFederationRefused as exc:
        raise _refusal(exc) from exc
    except OidcFederationConfigError as exc:
        raise _misconfigured(exc) from exc
    return OidcMobileStartResponse(
        ceremony_id=started.ceremony_id,
        issuer=started.issuer,
        client_id=started.client_id,
        redirect_uri=started.redirect_uri,
        audience=started.audience,
        scope=started.scope,
        nonce=started.nonce,
        code_challenge_method=started.code_challenge_method,
        expires_at=started.expires_at,
        expires_in_seconds=started.expires_in_seconds,
    )


@router.post(
    "/exchange",
    response_model=OidcMobileExchangeResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": OidcRefusalResponse},
        503: {"model": OidcRefusalResponse},
    },
)
def exchange_assertion(
    payload: OidcMobileExchangeRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> OidcMobileExchangeResponse:
    """Redeem one ceremony and return the normal Sub access/refresh pair."""

    try:
        issued = federation.exchange_mobile_assertion(
            db,
            federation.ExchangeMobileAssertionCommand(
                context=_context("Field mobile OIDC assertion exchange"),
                ceremony_id=payload.ceremony_id,
                id_token=payload.id_token,
                client_id=payload.client_id,
                redirect_uri=payload.redirect_uri,
            ),
            request=request,
        )
    except federation.OidcFederationRefused as exc:
        raise _refusal(exc) from exc
    except OidcFederationConfigError as exc:
        raise _misconfigured(exc) from exc
    return OidcMobileExchangeResponse(
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        token_type=issued.token_type,
        principal_type=issued.principal_type,
        principal_id=issued.principal_id,
        ceremony_id=issued.ceremony_id,
    )
