"""HTTP/session adapters for customer account hard deletion."""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import HTTPException, Request

from app.models.audit import AuditActorType
from app.services.audit_adapter import AuditActor
from app.services.customer_account_deletion import (
    SCOPE,
    CustomerAccountDeletionError,
    CustomerAccountDeletionOutcome,
    CustomerDeletionExpectation,
    CustomerDeletionPolicy,
    CustomerDeletionStatus,
    DeleteCustomerAccountCommand,
    delete_customer_account,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext


def customer_deletion_actor(request: Request) -> AuditActor:
    auth = getattr(request.state, "auth", None) or {}
    principal_id = str(auth.get("principal_id") or "").strip()
    if principal_id:
        kind = (
            AuditActorType.api_key
            if auth.get("principal_type") == "api_key"
            else AuditActorType.user
        )
        return AuditActor(kind, principal_id)
    from app.web.admin import get_current_user

    user = get_current_user(request)
    principal_id = str(user.get("principal_id") or "").strip() if user else ""
    if not principal_id:
        raise HTTPException(status_code=403, detail="Authorized actor is missing")
    return AuditActor.user(principal_id)


def delete_customer_from_adapter(
    *,
    customer_id: str,
    actor: AuditActor,
    policy: CustomerDeletionPolicy,
    expected_eligibility: CustomerDeletionExpectation | None = None,
) -> CustomerAccountDeletionOutcome:
    try:
        subscriber_id = UUID(customer_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="Subscriber not found") from exc
    command_id = uuid4()
    command = DeleteCustomerAccountCommand(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor=f"{actor.actor_type.value}:{actor.actor_id}",
            scope=SCOPE,
            reason="Administrative customer account hard deletion",
        ),
        actor=actor,
        subscriber_id=subscriber_id,
        policy=policy,
        expected_eligibility=expected_eligibility,
    )
    try:
        with db_session_adapter.owner_command_session() as command_db:
            return delete_customer_account(command_db, command=command)
    except CustomerAccountDeletionError as exc:
        status = 503 if exc.code.endswith((".busy", ".unavailable")) else 409
        raise HTTPException(
            status_code=status,
            detail=exc.message,
            headers={"Retry-After": "5"} if status == 503 else None,
        ) from None


def require_deleted(
    outcome: CustomerAccountDeletionOutcome, *, business: bool = False
) -> None:
    if outcome.status is CustomerDeletionStatus.DELETED:
        return
    if outcome.status is CustomerDeletionStatus.MISSING:
        raise HTTPException(status_code=404, detail="Subscriber not found")
    if outcome.status is CustomerDeletionStatus.ACTIVE:
        raise HTTPException(
            status_code=409, detail="Deactivate customer before deleting."
        )
    raise HTTPException(
        status_code=409,
        detail="Delete subscriptions before deleting business customer."
        if business
        else "Has associated subscriptions",
    )
