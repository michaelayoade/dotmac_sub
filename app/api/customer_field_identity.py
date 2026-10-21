"""Authenticated customer assignment-bound card and portrait adapters."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import customer_field_identity as identity
from app.services.auth_dependencies import require_user_auth

router = APIRouter(prefix="/work-orders", tags=["customer-field-identity"])


def _subscriber_id(auth: dict) -> UUID:
    if auth.get("principal_type") != "subscriber":
        raise HTTPException(status_code=403, detail="A customer account is required")
    try:
        return UUID(str(auth["subscriber_id"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=403, detail="A customer account is required"
        ) from exc


def _http_error(exc: identity.CustomerFieldIdentityError) -> HTTPException:
    return HTTPException(
        status_code=404 if exc.code.endswith("not_found") else 403,
        detail=exc.message,
        headers={"Cache-Control": "private, no-store"},
    )


@router.get(
    "/{work_order_id}/field-identity",
    response_model=identity.CustomerFieldIdentityOutcome,
)
def field_identity_card(
    work_order_id: UUID,
    response: Response,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> identity.CustomerFieldIdentityOutcome:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return identity.card(
            db,
            query=identity.CustomerFieldIdentityQuery(
                subscriber_id=_subscriber_id(auth), work_order_id=work_order_id
            ),
        )
    except identity.CustomerFieldIdentityError as exc:
        raise _http_error(exc) from exc


@router.get("/{work_order_id}/field-identity/portrait")
def field_identity_portrait(
    work_order_id: UUID,
    assignment_generation: UUID,
    portrait_revision: int,
    auth: dict = Depends(require_user_auth),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    try:
        stream = identity.portrait(
            db,
            query=identity.CustomerFieldPortraitQuery(
                subscriber_id=_subscriber_id(auth),
                work_order_id=work_order_id,
                assignment_generation=assignment_generation,
                portrait_revision=portrait_revision,
            ),
        )
    except identity.CustomerFieldIdentityError as exc:
        raise _http_error(exc) from exc
    return StreamingResponse(
        stream.chunks,
        media_type=stream.content_type or "application/octet-stream",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        },
    )
