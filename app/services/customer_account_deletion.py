"""Atomic owner of administrative customer account hard deletion."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.catalog import Subscription
from app.models.subscriber import Subscriber
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "customer.account_deletion"
CONCERN = "customer account hard deletion"
SCOPE = "customer:delete"
CUSTOMER_ACCOUNT_DELETION_DEFINITION = OwnerCommandDefinition(
    owner=OWNER, concern=CONCERN, name="delete_customer_account"
)


class CustomerDeletionPolicy(StrEnum):
    WEB_PERSON = "web_person"
    WEB_BUSINESS = "web_business"
    WEB_BULK = "web_bulk"
    JSON = "json"


class CustomerDeletionStatus(StrEnum):
    DELETED = "deleted"
    MISSING = "missing"
    ACTIVE = "active"
    SUBSCRIBED = "subscribed"


@dataclass(frozen=True, slots=True)
class CustomerDeletionExpectation:
    subscriber_id: UUID
    is_active: bool
    has_subscriptions: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class DeleteCustomerAccountCommand:
    context: CommandContext = field(repr=False)
    actor: AuditActor = field(repr=False)
    subscriber_id: UUID
    policy: CustomerDeletionPolicy
    expected_eligibility: CustomerDeletionExpectation | None = None


@dataclass(frozen=True, slots=True)
class CustomerAccountDeletionOutcome:
    subscriber_id: UUID
    status: CustomerDeletionStatus


class CustomerAccountDeletionError(DomainError):
    """Sanitized refusal, emitted only after executor rollback."""


def require_customer_deletion_participant(
    db: Session, *, context: CommandContext, actor: AuditActor
) -> None:
    from app.services.owner_commands import (
        current_command_context,
        current_command_definition,
        owner_command_active,
    )

    if (
        not isinstance(context, CommandContext)
        or not isinstance(actor, AuditActor)
        or not owner_command_active(db, owner=OWNER)
        or current_command_definition(db) != CUSTOMER_ACCOUNT_DELETION_DEFINITION
        or current_command_context(db) != context
        or context.scope != SCOPE
        or actor.actor_type not in {AuditActorType.user, AuditActorType.api_key}
        or context.actor != f"{actor.actor_type.value}:{actor.actor_id}"
    ):
        raise CustomerAccountDeletionError(
            code=f"{OWNER}.invalid_participant",
            message="Exact customer deletion context required.",
        )


def _lock_orm_children(db: Session, *, subscriber_id: UUID) -> None:
    """NOWAIT every row the legacy Subscriber ORM deletion mutates."""
    from app.models.catalog import AccessCredential
    from app.models.collections import DunningCase
    from app.models.network import CPEDevice, IPAssignment, OntAssignment
    from app.models.party import (
        SubscriberContactPointProjection,
        SubscriberContactRelationshipProjection,
    )
    from app.models.provisioning import ServiceOrder
    from app.models.subscriber import (
        Address,
        SubscriberChannel,
        SubscriberContact,
        SubscriberCustomField,
        SubscriberNINVerification,
    )

    tuple(
        db.scalars(
            select(Address.id)
            .where(Address.subscriber_id == subscriber_id)
            .order_by(Address.id)
            .with_for_update(nowait=True)
        )
    )
    tuple(
        db.scalars(
            select(SubscriberCustomField.id)
            .where(SubscriberCustomField.subscriber_id == subscriber_id)
            .order_by(SubscriberCustomField.id)
            .with_for_update(nowait=True)
        )
    )
    tuple(
        db.scalars(
            select(SubscriberChannel.id)
            .where(SubscriberChannel.subscriber_id == subscriber_id)
            .order_by(SubscriberChannel.id)
            .with_for_update(nowait=True)
        )
    )
    tuple(
        db.scalars(
            select(SubscriberContact.id)
            .where(SubscriberContact.subscriber_id == subscriber_id)
            .order_by(SubscriberContact.id)
            .with_for_update(nowait=True)
        )
    )
    tuple(
        db.scalars(
            select(SubscriberNINVerification.id)
            .where(SubscriberNINVerification.subscriber_id == subscriber_id)
            .order_by(SubscriberNINVerification.id)
            .with_for_update(nowait=True)
        )
    )
    tuple(
        db.scalars(
            select(Subscription.id)
            .where(Subscription.subscriber_id == subscriber_id)
            .order_by(Subscription.id)
            .with_for_update(nowait=True)
        )
    )
    tuple(
        db.scalars(
            select(AccessCredential.id)
            .where(AccessCredential.subscriber_id == subscriber_id)
            .order_by(AccessCredential.id)
            .with_for_update(nowait=True)
        )
    )
    tuple(
        db.scalars(
            select(ServiceOrder.id)
            .where(ServiceOrder.subscriber_id == subscriber_id)
            .order_by(ServiceOrder.id)
            .with_for_update(nowait=True)
        )
    )
    tuple(
        db.scalars(
            select(CPEDevice.id)
            .where(CPEDevice.subscriber_id == subscriber_id)
            .order_by(CPEDevice.id)
            .with_for_update(nowait=True)
        )
    )
    tuple(
        db.scalars(
            select(IPAssignment.id)
            .where(IPAssignment.subscriber_id == subscriber_id)
            .order_by(IPAssignment.id)
            .with_for_update(nowait=True)
        )
    )
    tuple(
        db.scalars(
            select(OntAssignment.id)
            .where(OntAssignment.subscriber_id == subscriber_id)
            .order_by(OntAssignment.id)
            .with_for_update(nowait=True)
        )
    )
    tuple(
        db.scalars(
            select(DunningCase.id)
            .where(DunningCase.account_id == subscriber_id)
            .order_by(DunningCase.id)
            .with_for_update(nowait=True)
        )
    )
    contact_ids = select(SubscriberContact.id).where(
        SubscriberContact.subscriber_id == subscriber_id
    )
    tuple(
        db.scalars(
            select(SubscriberContactRelationshipProjection.id)
            .where(
                SubscriberContactRelationshipProjection.subscriber_contact_id.in_(
                    contact_ids
                )
            )
            .order_by(SubscriberContactRelationshipProjection.id)
            .with_for_update(nowait=True)
        )
    )
    tuple(
        db.scalars(
            select(SubscriberContactPointProjection.id)
            .where(
                SubscriberContactPointProjection.subscriber_contact_id.in_(contact_ids)
            )
            .order_by(SubscriberContactPointProjection.id)
            .with_for_update(nowait=True)
        )
    )


def _refuse_referral_blocker(db: Session, *, subscriber_id: UUID) -> None:
    from app.models.referral_native import Referral

    if (
        db.scalar(
            select(Referral.id)
            .where(
                or_(
                    Referral.referrer_subscriber_id == subscriber_id,
                    Referral.referred_subscriber_id == subscriber_id,
                )
            )
            .limit(1)
        )
        is not None
    ):
        raise CustomerAccountDeletionError(
            code=f"{OWNER}.blocked",
            message="Cannot delete customer. Linked records exist.",
        )


def delete_customer_account(
    db: Session, *, command: DeleteCustomerAccountCommand
) -> CustomerAccountDeletionOutcome:
    if (
        not isinstance(command, DeleteCustomerAccountCommand)
        or not isinstance(command.subscriber_id, UUID)
        or not isinstance(command.policy, CustomerDeletionPolicy)
        or (
            command.policy is CustomerDeletionPolicy.WEB_BULK
            and command.expected_eligibility is None
        )
        or (
            command.expected_eligibility is not None
            and (
                not isinstance(
                    command.expected_eligibility, CustomerDeletionExpectation
                )
                or command.expected_eligibility.subscriber_id != command.subscriber_id
                or type(command.expected_eligibility.is_active) is not bool
                or type(command.expected_eligibility.has_subscriptions) is not bool
            )
        )
        or not isinstance(command.context, CommandContext)
        or not isinstance(command.actor, AuditActor)
        or command.actor.actor_type not in {AuditActorType.user, AuditActorType.api_key}
        or command.context.actor
        != f"{command.actor.actor_type.value}:{command.actor.actor_id}"
        or command.context.scope != SCOPE
    ):
        raise CustomerAccountDeletionError(
            code=f"{OWNER}.invalid_command",
            message="Trusted administrative deletion command required.",
        )

    def operation() -> CustomerAccountDeletionOutcome:
        from app.services.api_key_administration import (
            CaptureCustomerKeysCommand,
            CustomerKeyDeletionCommand,
            capture_and_lock_customer_keys,
            stage_customer_key_deletion,
            validate_customer_key_capture,
        )
        from app.services.cpe_subscriber_retirement import (
            RetireSubscriberCPECommand,
            stage_subscriber_cpe_retirement,
        )
        from app.services.credential_administration_mutation import (
            CustomerAuthDeletionCommand,
            stage_customer_auth_deletion,
        )
        from app.services.ip_assignment_lifecycle import (
            ReleaseSubscriberIPAssignmentsCommand,
            stage_subscriber_ip_assignment_release,
        )

        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SELECT set_config('lock_timeout', '5s', true)"))
        cleanup_auth = command.policy is not CustomerDeletionPolicy.JSON
        locked_keys: tuple[UUID, ...] = ()
        if cleanup_auth:
            locked_keys = capture_and_lock_customer_keys(
                db,
                command=CaptureCustomerKeysCommand(
                    context=command.context,
                    actor=command.actor,
                    subscriber_id=command.subscriber_id,
                ),
            ).locked_key_ids
        subscriber: Subscriber | None = db.scalars(
            select(Subscriber)
            .where(Subscriber.id == command.subscriber_id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        ).first()
        if subscriber is None:
            return CustomerAccountDeletionOutcome(
                command.subscriber_id, CustomerDeletionStatus.MISSING
            )
        if cleanup_auth:
            validate_customer_key_capture(
                db,
                command=CustomerKeyDeletionCommand(
                    context=command.context,
                    actor=command.actor,
                    subscriber_id=command.subscriber_id,
                    locked_key_ids=locked_keys,
                ),
            )
        has_subscriptions = (
            db.scalar(
                select(Subscription.id)
                .where(Subscription.subscriber_id == command.subscriber_id)
                .limit(1)
            )
            is not None
        )
        if (
            command.expected_eligibility is not None
            and command.expected_eligibility
            != CustomerDeletionExpectation(
                command.subscriber_id, bool(subscriber.is_active), has_subscriptions
            )
        ):
            raise CustomerAccountDeletionError(
                code=f"{OWNER}.stale_impact",
                message="Customer deletion impact changed. Preview again.",
            )
        if cleanup_auth:
            if (
                command.policy
                in {
                    CustomerDeletionPolicy.WEB_BUSINESS,
                    CustomerDeletionPolicy.WEB_BULK,
                }
                and has_subscriptions
            ):
                return CustomerAccountDeletionOutcome(
                    command.subscriber_id, CustomerDeletionStatus.SUBSCRIBED
                )
            if subscriber.is_active:
                return CustomerAccountDeletionOutcome(
                    command.subscriber_id, CustomerDeletionStatus.ACTIVE
                )
            _refuse_referral_blocker(db, subscriber_id=command.subscriber_id)
            stage_customer_auth_deletion(
                db,
                command=CustomerAuthDeletionCommand(
                    context=command.context,
                    actor=command.actor,
                    subscriber_id=command.subscriber_id,
                ),
            )
            stage_customer_key_deletion(
                db,
                command=CustomerKeyDeletionCommand(
                    context=command.context,
                    actor=command.actor,
                    subscriber_id=command.subscriber_id,
                    locked_key_ids=locked_keys,
                ),
            )
        if not cleanup_auth:
            _refuse_referral_blocker(db, subscriber_id=command.subscriber_id)
        stage_subscriber_ip_assignment_release(
            db,
            command=ReleaseSubscriberIPAssignmentsCommand(
                context=command.context,
                actor=command.actor,
                subscriber_id=command.subscriber_id,
            ),
        )
        stage_subscriber_cpe_retirement(
            db,
            command=RetireSubscriberCPECommand(
                context=command.context,
                actor=command.actor,
                subscriber_id=command.subscriber_id,
            ),
        )
        _lock_orm_children(db, subscriber_id=command.subscriber_id)
        db.delete(subscriber)
        stage_audit_event(
            db,
            action="customer.account.deleted",
            entity_type="subscriber",
            entity_id=str(command.subscriber_id),
            actor=command.actor,
            details={
                "schema_version": 1,
                "policy": command.policy.value,
                "command_id": str(command.context.command_id),
                "correlation_id": str(command.context.correlation_id),
            },
        )
        db.flush()
        return CustomerAccountDeletionOutcome(
            command.subscriber_id, CustomerDeletionStatus.DELETED
        )

    try:
        return execute_owner_command(
            db,
            definition=CUSTOMER_ACCOUNT_DELETION_DEFINITION,
            context=command.context,
            operation=operation,
        )
    except IntegrityError as exc:
        raise CustomerAccountDeletionError(
            code=f"{OWNER}.blocked",
            message="Cannot delete customer. Linked records exist.",
        ) from exc
    except SQLAlchemyError as exc:
        state = getattr(exc.orig, "sqlstate", None) if hasattr(exc, "orig") else None
        suffix = "busy" if state in {"55P03", "40P01", "40001"} else "unavailable"
        raise CustomerAccountDeletionError(
            code=f"{OWNER}.{suffix}",
            message="Customer deletion is temporarily unavailable.",
        ) from exc
    except DomainError:
        raise
    except Exception as exc:
        raise CustomerAccountDeletionError(
            code=f"{OWNER}.unavailable",
            message="Customer deletion is temporarily unavailable.",
        ) from exc
