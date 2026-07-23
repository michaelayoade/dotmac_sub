"""Canonical account billing-approval and lifecycle coordination.

``Subscriber.billing_enabled`` is an admission fact, not an independent
runtime switch.  Revoking approval on an existing account therefore moves its
non-terminal services through the account lifecycle owner to ``disabled``.
Re-approval restores service only when this owner created the administrative
disable; unrelated lifecycle decisions remain intact.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.account_lifecycle import (
    TERMINAL_STATUSES,
    clear_account_lifecycle_override,
    compute_account_status,
    enable_subscription,
    transition_account_status,
)
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

BILLING_APPROVAL_WRITE_SCOPE = "customer:billing-approval:write"
_SOURCE_PREFIX = "billing_approval:"

_CHANGE_COMMAND = OwnerCommandDefinition(
    owner="customer.billing_approval",
    concern="atomic account billing-approval and lifecycle transition",
    name="change_account_billing_approval",
)
_RECONCILE_COMMAND = OwnerCommandDefinition(
    owner="customer.billing_approval",
    concern="account billing-approval drift reconciliation",
    name="reconcile_account_billing_approval",
)


class BillingApprovalAction(StrEnum):
    unchanged = "unchanged"
    approved = "approved"
    disabled = "disabled"
    restored = "restored"
    treatment_aligned = "treatment_aligned"


class AccountBillingApprovalError(DomainError):
    """Stable transport-neutral billing-approval command failure."""


def _error(suffix: str, message: str, **details: object) -> AccountBillingApprovalError:
    return AccountBillingApprovalError(
        code=f"customer.billing_approval.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class ChangeAccountBillingApprovalCommand:
    context: CommandContext
    account_id: UUID
    approved: bool


@dataclass(frozen=True, slots=True)
class ReconcileAccountBillingApprovalCommand:
    context: CommandContext
    account_id: UUID


@dataclass(frozen=True, slots=True)
class BillingApprovalOutcome:
    account_id: UUID
    approved: bool
    prior_approved: bool
    prior_status: SubscriberStatus
    status: SubscriberStatus
    action: BillingApprovalAction
    affected_subscription_ids: tuple[UUID, ...]


def _actor(context: CommandContext) -> tuple[AuditActorType, str]:
    prefix, separator, identifier = context.actor.partition(":")
    actor_id = identifier if separator and identifier else context.actor
    if prefix == "api_key":
        return AuditActorType.api_key, actor_id
    if prefix == "user":
        return AuditActorType.user, actor_id
    if prefix == "service":
        return AuditActorType.service, actor_id
    return AuditActorType.system, actor_id


def _lock_account_and_subscriptions(
    db: Session, account_id: UUID
) -> tuple[Subscriber, list[Subscription]]:
    account = db.scalar(
        select(Subscriber).where(Subscriber.id == account_id).with_for_update()
    )
    if account is None:
        raise _error(
            "account_not_found",
            "The subscriber account was not found.",
            account_id=str(account_id),
        )
    subscriptions = list(
        db.scalars(
            select(Subscription)
            .where(Subscription.subscriber_id == account.id)
            .order_by(Subscription.id)
            .with_for_update()
        ).all()
    )
    return account, subscriptions


def _validate_context(context: CommandContext) -> None:
    if context.scope != BILLING_APPROVAL_WRITE_SCOPE:
        raise _error(
            "invalid_scope",
            "Billing-approval write scope is required.",
        )
    if not context.reason.strip():
        raise _error("invalid_reason", "A billing-approval reason is required.")


def _was_disabled_by_billing_approval(account: Subscriber) -> bool:
    return account.lifecycle_override_status == SubscriberStatus.disabled and str(
        account.lifecycle_override_source or ""
    ).startswith(_SOURCE_PREFIX)


def _stage_evidence(
    db: Session,
    *,
    account: Subscriber,
    context: CommandContext,
    prior_approved: bool,
    prior_status: SubscriberStatus,
    action: BillingApprovalAction,
    affected_subscription_ids: tuple[UUID, ...],
) -> None:
    actor_type, actor_id = _actor(context)
    metadata: dict[str, object] = {
        "schema_version": 1,
        "account_id": str(account.id),
        "approved": bool(account.billing_enabled),
        "prior_approved": prior_approved,
        "prior_status": prior_status.value,
        "status": account.status.value,
        "action": action.value,
        "affected_subscription_ids": [
            str(subscription_id) for subscription_id in affected_subscription_ids
        ],
        "reason": context.reason,
        "command_id": str(context.command_id),
        "correlation_id": str(context.correlation_id),
    }
    stage_audit_event(
        db,
        action="customer.billing_approval_changed",
        entity_type="subscriber",
        entity_id=str(account.id),
        actor_type=actor_type,
        actor_id=actor_id,
        request_id=str(context.correlation_id),
        metadata=metadata,
    )
    emit_event(
        db,
        EventType.subscriber_billing_approval_changed,
        metadata,
        actor=context.actor,
        subscriber_id=account.id,
        account_id=account.id,
    )


def _outcome(
    *,
    account: Subscriber,
    prior_approved: bool,
    prior_status: SubscriberStatus,
    action: BillingApprovalAction,
    affected_subscription_ids: tuple[UUID, ...],
) -> BillingApprovalOutcome:
    return BillingApprovalOutcome(
        account_id=account.id,
        approved=bool(account.billing_enabled),
        prior_approved=prior_approved,
        prior_status=prior_status,
        status=account.status,
        action=action,
        affected_subscription_ids=affected_subscription_ids,
    )


def change_account_billing_approval(
    db: Session, command: ChangeAccountBillingApprovalCommand
) -> BillingApprovalOutcome:
    """Apply one explicit approval transition and its lifecycle consequence."""

    def operation() -> BillingApprovalOutcome:
        _validate_context(command.context)
        account, subscriptions = _lock_account_and_subscriptions(db, command.account_id)
        prior_approved = bool(account.billing_enabled)
        prior_status = account.status
        source = f"{_SOURCE_PREFIX}{command.context.command_id}"

        if not command.approved:
            affected = tuple(
                subscription.id
                for subscription in subscriptions
                if subscription.status not in TERMINAL_STATUSES
                and subscription.status != SubscriptionStatus.disabled
            )
            already_aligned = (
                not prior_approved
                and prior_status == SubscriberStatus.disabled
                and _was_disabled_by_billing_approval(account)
                and not affected
            )
            if already_aligned:
                return _outcome(
                    account=account,
                    prior_approved=prior_approved,
                    prior_status=prior_status,
                    action=BillingApprovalAction.unchanged,
                    affected_subscription_ids=(),
                )
            account.billing_enabled = False
            transition_account_status(
                db,
                str(account.id),
                SubscriberStatus.disabled,
                reason=command.context.reason,
                source=source,
                preserve_locks=True,
            )
            action = BillingApprovalAction.disabled
        else:
            restore_owned_disable = _was_disabled_by_billing_approval(account)
            affected = (
                tuple(
                    subscription.id
                    for subscription in subscriptions
                    if subscription.status == SubscriptionStatus.disabled
                )
                if restore_owned_disable
                else ()
            )
            account.billing_enabled = True
            if restore_owned_disable:
                clear_account_lifecycle_override(
                    db,
                    str(account.id),
                    reason=command.context.reason,
                    source=source,
                )
                for subscription in subscriptions:
                    if subscription.status == SubscriptionStatus.disabled:
                        enable_subscription(
                            db,
                            str(subscription.id),
                            reason=command.context.reason,
                            source=source,
                        )
                compute_account_status(db, str(account.id))
                action = BillingApprovalAction.restored
            else:
                action = (
                    BillingApprovalAction.unchanged
                    if prior_approved
                    else BillingApprovalAction.approved
                )

        db.flush()
        if (
            prior_approved != bool(account.billing_enabled)
            or prior_status != account.status
            or action != BillingApprovalAction.unchanged
        ):
            _stage_evidence(
                db,
                account=account,
                context=command.context,
                prior_approved=prior_approved,
                prior_status=prior_status,
                action=action,
                affected_subscription_ids=affected,
            )
        return _outcome(
            account=account,
            prior_approved=prior_approved,
            prior_status=prior_status,
            action=action,
            affected_subscription_ids=affected,
        )

    return execute_owner_command(
        db,
        definition=_CHANGE_COMMAND,
        context=command.context,
        operation=operation,
    )


def find_billing_approval_drift_account_ids(
    db: Session, *, limit: int = 200
) -> tuple[UUID, ...]:
    """Return active-service accounts excluded by the approval fact."""
    rows = db.scalars(
        select(Subscriber.id)
        .join(Subscription, Subscription.subscriber_id == Subscriber.id)
        .where(
            Subscriber.billing_enabled.is_(False),
            Subscription.status == SubscriptionStatus.active,
        )
        .distinct()
        .order_by(Subscriber.id)
        .limit(max(1, min(limit, 1000)))
    ).all()
    return tuple(rows)


def reconcile_account_billing_approval(
    db: Session, command: ReconcileAccountBillingApprovalCommand
) -> BillingApprovalOutcome:
    """Repair one legacy active/unapproved account without inventing a waiver.

    An effective treatment already owns suppression of customer billing, so a
    redundant false approval flag is repaired to true.  Otherwise the explicit
    false fact wins fail-safe and the account is administratively disabled.
    """

    def operation() -> BillingApprovalOutcome:
        _validate_context(command.context)
        account, subscriptions = _lock_account_and_subscriptions(db, command.account_id)
        prior_approved = bool(account.billing_enabled)
        prior_status = account.status
        active = [
            subscription
            for subscription in subscriptions
            if subscription.status == SubscriptionStatus.active
        ]
        affected = tuple(subscription.id for subscription in active)
        if prior_approved or not active:
            return _outcome(
                account=account,
                prior_approved=prior_approved,
                prior_status=prior_status,
                action=BillingApprovalAction.unchanged,
                affected_subscription_ids=affected,
            )

        from app.services.subscription_billing_treatments import (
            BillingTreatmentDecisionStatus,
            resolve_subscription_billing_treatments,
        )

        treatments = resolve_subscription_billing_treatments(db, active)
        all_treated = all(
            treatments[subscription.id].status
            == BillingTreatmentDecisionStatus.effective
            for subscription in active
        )
        source = f"{_SOURCE_PREFIX}reconcile:{command.context.command_id}"
        if all_treated:
            account.billing_enabled = True
            action = BillingApprovalAction.treatment_aligned
        else:
            transition_account_status(
                db,
                str(account.id),
                SubscriberStatus.disabled,
                reason=command.context.reason,
                source=source,
                preserve_locks=True,
            )
            action = BillingApprovalAction.disabled
        db.flush()
        _stage_evidence(
            db,
            account=account,
            context=command.context,
            prior_approved=prior_approved,
            prior_status=prior_status,
            action=action,
            affected_subscription_ids=affected,
        )
        return _outcome(
            account=account,
            prior_approved=prior_approved,
            prior_status=prior_status,
            action=action,
            affected_subscription_ids=affected,
        )

    return execute_owner_command(
        db,
        definition=_RECONCILE_COMMAND,
        context=command.context,
        operation=operation,
    )
