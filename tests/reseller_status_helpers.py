"""Typed reseller account-status command helpers for behavior tests."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.services import reseller_portal
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext


def preview_status_confirmation(
    db: Session,
    *,
    reseller_id: UUID,
    account_id: UUID,
    action: str,
    fingerprint: str,
) -> dict | None:
    return reseller_portal.preview_customer_account_status_confirmation(
        db,
        reseller_portal.PreviewCustomerAccountStatusConfirmationRequest(
            reseller_id=reseller_id,
            account_id=account_id,
            action=reseller_portal.ResellerAccountStatusAction(action),
            expected_preview_fingerprint=fingerprint,
        ),
    )


def confirm_status_action(
    db: Session,
    *,
    reseller_id: UUID,
    account_id: UUID,
    action: str,
    fingerprint: str,
    idempotency_key: str,
    actor: str = "test:reseller_status",
) -> dict | None:
    command = reseller_portal.ConfirmCustomerAccountStatusCommand(
        context=CommandContext.system(
            actor=actor,
            scope=str(account_id),
            reason=f"test_confirm_{action}_account",
            idempotency_key=idempotency_key,
        ),
        reseller_id=reseller_id,
        account_id=account_id,
        action=reseller_portal.ResellerAccountStatusAction(action),
        expected_preview_fingerprint=fingerprint,
    )
    db_session_adapter.release_read_transaction(db)
    return reseller_portal.confirm_customer_account_status_action(db, command)


def confirm_current_status_action(
    db: Session,
    *,
    reseller_id: UUID,
    account_id: UUID,
    action: str,
) -> dict | None:
    detail = reseller_portal.get_account_detail(
        db,
        str(reseller_id),
        str(account_id),
    )
    assert detail is not None
    proposal = preview_status_confirmation(
        db,
        reseller_id=reseller_id,
        account_id=account_id,
        action=action,
        fingerprint=detail["status_actions"][action]["fingerprint"],
    )
    assert proposal is not None
    return confirm_status_action(
        db,
        reseller_id=reseller_id,
        account_id=account_id,
        action=action,
        fingerprint=proposal["preview_fingerprint"],
        idempotency_key=proposal["idempotency_key"],
    )
