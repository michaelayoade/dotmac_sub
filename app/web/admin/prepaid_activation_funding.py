"""Admin routes for the prepaid activation funding override.

Adapter only: authorization is checked here and again by the owner
(``financial.prepaid_activation_funding_guard``), which validates the actor,
reason, quarantine state, and records audit evidence in its own transaction.
"""

from urllib.parse import quote_plus
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_prepaid_activation_funding as web_override
from app.services.auth_dependencies import has_permission, require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.prepaid_activation_funding_guard import (
    OVERRIDE_PERMISSION,
    GrantPrepaidActivationFundingOverrideCommand,
    RevokePrepaidActivationFundingOverrideCommand,
    grant_prepaid_activation_funding_override,
    revoke_prepaid_activation_funding_override,
)

router = APIRouter(prefix="/customers", tags=["web-admin-customers"])


def _staff_actor(request: Request) -> tuple[dict, UUID]:
    auth = getattr(request.state, "auth", None) or {}
    if str(auth.get("principal_type") or "") != "system_user":
        raise HTTPException(
            status_code=403,
            detail="A signed-in staff user is required for this decision",
        )
    try:
        return auth, UUID(str(auth.get("principal_id") or ""))
    except ValueError as exc:
        raise HTTPException(
            status_code=403, detail="Authorized actor is missing"
        ) from exc


def _redirect(return_to: str | None, account_id: UUID, *, key: str, message: str):
    target = web_override.safe_return_path(
        return_to, fallback=f"/admin/customers/person/{account_id}"
    )
    path, _, fragment = target.partition("#")
    separator = "&" if "?" in path else "?"
    url = f"{path}{separator}{key}={quote_plus(message)}"
    if fragment:
        url = f"{url}#{fragment}"
    return RedirectResponse(url=url, status_code=303)


@router.post(
    "/accounts/{account_id}/prepaid-activation-override",
    dependencies=[Depends(require_permission(OVERRIDE_PERMISSION))],
)
def grant_prepaid_activation_override(
    request: Request,
    account_id: UUID,
    reason: str = Form(""),
    confirmed: str | None = Form(None),
    return_to: str | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    auth, actor_id = _staff_actor(request)
    if confirmed != "yes":
        return _redirect(
            return_to,
            account_id,
            key="prepaid_funding_error",
            message=(
                "Confirm that the account stays excluded from prepaid enforcement "
                "until its opening is captured."
            ),
        )
    permitted = has_permission(auth, db, OVERRIDE_PERMISSION)
    context = web_override.override_command_context(
        actor_system_user_id=actor_id,
        account_id=account_id,
        action="grant",
        reason="Staff admitted prepaid activation before funding opening review",
    )
    try:
        db_session_adapter.release_read_transaction(db)
        grant_prepaid_activation_funding_override(
            db,
            GrantPrepaidActivationFundingOverrideCommand(
                context=context,
                account_id=account_id,
                actor_system_user_id=actor_id,
                permission_granted=permitted,
                reason=reason,
            ),
        )
    except DomainError as exc:
        return _redirect(
            return_to, account_id, key="prepaid_funding_error", message=exc.message
        )
    return _redirect(
        return_to,
        account_id,
        key="prepaid_funding_notice",
        message=(
            "Prepaid activation override recorded. The account remains "
            "funding-quarantined until its reviewed opening is captured."
        ),
    )


@router.post(
    "/accounts/{account_id}/prepaid-activation-override/revoke",
    dependencies=[Depends(require_permission(OVERRIDE_PERMISSION))],
)
def revoke_prepaid_activation_override(
    request: Request,
    account_id: UUID,
    reason: str = Form(""),
    return_to: str | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    auth, actor_id = _staff_actor(request)
    permitted = has_permission(auth, db, OVERRIDE_PERMISSION)
    context = web_override.override_command_context(
        actor_system_user_id=actor_id,
        account_id=account_id,
        action="revoke",
        reason="Staff revoked prepaid activation funding override",
    )
    try:
        db_session_adapter.release_read_transaction(db)
        revoke_prepaid_activation_funding_override(
            db,
            RevokePrepaidActivationFundingOverrideCommand(
                context=context,
                account_id=account_id,
                actor_system_user_id=actor_id,
                permission_granted=permitted,
                reason=reason,
            ),
        )
    except DomainError as exc:
        return _redirect(
            return_to, account_id, key="prepaid_funding_error", message=exc.message
        )
    return _redirect(
        return_to,
        account_id,
        key="prepaid_funding_notice",
        message="Prepaid activation override revoked.",
    )
