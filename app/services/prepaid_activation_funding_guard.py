"""Prepaid activation admission against the prepaid funding quarantine.

A legacy account (it existed when customer-subledger authority activated)
with no reviewed funding baseline and no subledger opening is in the prepaid
funding quarantine: ``prepaid_funding_incomplete_source_account_ids``. Every
money-based prepaid suspension/restoration excludes it, and its arrival in the
prepaid cohort grows ``billing_prepaid_funding_quarantined_accounts`` and fires
``SubPrepaidFundingQuarantineGrowing``. Nothing stopped staff giving such an
account its first prepaid subscription, so the quarantine could only be
noticed after the fact.

This owner answers one question at the moment prepaid service is about to
start (subscription create, pending -> active activation, or a billing-mode
change to prepaid): would the account be funding-quarantined, why, and which
runbook clears it. It reuses the quarantine resolver and the carried-source
identity classifier; it does not re-derive either.

Admission is fail-closed. The only bypass is a durable, permission-gated,
attributable ``PrepaidActivationFundingOverride``. An override never changes
the quarantine computation: the account stays excluded from money actions and
stays counted by the quarantine signal until its opening is captured.

Before customer-subledger authority activation every account without a
baseline is in the incomplete-source set by design (the complete-cohort
opening capture owns all of them), so the per-account guard is inert there.
It applies once authority is active, when the quarantine is a closed legacy
cohort and any growth is a regression.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeVar
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.catalog import BillingMode, Subscription
from app.models.customer_subledger import CustomerSubledgerAuthorityCutover
from app.models.prepaid_funding import PrepaidActivationFundingOverride
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.schemas.audit import AuditEventCreate
from app.services import audit as audit_service
from app.services.billing.opening_balance_history import (
    OpeningBalanceHistoryError,
    OpeningBalanceSourceIdentityDisposition,
    OpeningBalanceSourceIdentityQuery,
    classify_opening_balance_source_identities,
)
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.prepaid_funding_reconstruction import (
    LEGACY_FINANCIAL_HANDOFF_AT,
    default_prepaid_funding_currency,
    prepaid_funding_incomplete_source_account_ids,
)

logger = logging.getLogger(__name__)

ResultT = TypeVar("ResultT")

OWNER = "financial.prepaid_activation_funding_guard"
CONCERN = "prepaid activation funding override decision"
OVERRIDE_PERMISSION = "billing:prepaid_funding:activation_override"
MIN_OVERRIDE_REASON_LENGTH = 10
MAX_OVERRIDE_REASON_LENGTH = 1000
_GRANT_AUDIT_ACTION = "prepaid_activation_funding_override_granted"
_REVOKE_AUDIT_ACTION = "prepaid_activation_funding_override_revoked"
_ADMITTED_AUDIT_ACTION = "prepaid_activation_admitted_by_funding_override"


class PrepaidActivationEntryPoint(StrEnum):
    """Where prepaid service is about to start for an account."""

    subscription_create = "subscription_create"
    subscription_activation = "subscription_activation"
    subscription_billing_mode_change = "subscription_billing_mode_change"
    account_billing_mode_change = "account_billing_mode_change"
    bulk_provisioning_activation = "bulk_provisioning_activation"


class PrepaidFundingQuarantineReason(StrEnum):
    """Closed reasons an account lacks prepaid funding authority."""

    migrated_opening_missing = "migrated_opening_missing"
    carried_source_identity_unresolved = "carried_source_identity_unresolved"
    reviewed_native_opening_missing = "reviewed_native_opening_missing"
    native_after_handoff_opening_missing = "native_after_handoff_opening_missing"
    source_identity_unclassifiable = "source_identity_unclassifiable"


class PrepaidFundingRemediationRunbook(StrEnum):
    """Repository runbook that clears one quarantine reason."""

    reviewed_migrated_opening_repair = (
        "docs/runbooks/REVIEWED_MIGRATED_PREPAID_OPENING_REPAIR.md"
    )
    prepaid_funding_audit_restore = "docs/runbooks/PREPAID_FUNDING_AUDIT_RESTORE.md"
    native_prepaid_opening_repair = "docs/runbooks/NATIVE_PREPAID_OPENING_REPAIR.md"


_REASON_RUNBOOK: dict[
    PrepaidFundingQuarantineReason, PrepaidFundingRemediationRunbook
] = {
    PrepaidFundingQuarantineReason.migrated_opening_missing: (
        PrepaidFundingRemediationRunbook.reviewed_migrated_opening_repair
    ),
    PrepaidFundingQuarantineReason.carried_source_identity_unresolved: (
        PrepaidFundingRemediationRunbook.prepaid_funding_audit_restore
    ),
    PrepaidFundingQuarantineReason.reviewed_native_opening_missing: (
        PrepaidFundingRemediationRunbook.prepaid_funding_audit_restore
    ),
    PrepaidFundingQuarantineReason.native_after_handoff_opening_missing: (
        PrepaidFundingRemediationRunbook.native_prepaid_opening_repair
    ),
    PrepaidFundingQuarantineReason.source_identity_unclassifiable: (
        PrepaidFundingRemediationRunbook.prepaid_funding_audit_restore
    ),
}

_REASON_SUMMARY: dict[PrepaidFundingQuarantineReason, str] = {
    PrepaidFundingQuarantineReason.migrated_opening_missing: (
        "Splynx-linked legacy account has no reviewed funding baseline or "
        "subledger opening"
    ),
    PrepaidFundingQuarantineReason.carried_source_identity_unresolved: (
        "account was created before the legacy financial handoff without a "
        "retained Splynx identity, and its native provenance has not been "
        "adjudicated"
    ),
    PrepaidFundingQuarantineReason.reviewed_native_opening_missing: (
        "pre-handoff native provenance is adjudicated but the reviewed opening "
        "has not been materialized"
    ),
    PrepaidFundingQuarantineReason.native_after_handoff_opening_missing: (
        "Sub-native account existed at subledger authority activation but its "
        "opening was never captured"
    ),
    PrepaidFundingQuarantineReason.source_identity_unclassifiable: (
        "account source identity evidence is inconsistent and must be reviewed"
    ),
}

_DISPOSITION_REASON: dict[
    OpeningBalanceSourceIdentityDisposition, PrepaidFundingQuarantineReason
] = {
    OpeningBalanceSourceIdentityDisposition.migrated_identity_present: (
        PrepaidFundingQuarantineReason.migrated_opening_missing
    ),
    OpeningBalanceSourceIdentityDisposition.unresolved_carried_identity: (
        PrepaidFundingQuarantineReason.carried_source_identity_unresolved
    ),
    OpeningBalanceSourceIdentityDisposition.native_before_handoff: (
        PrepaidFundingQuarantineReason.reviewed_native_opening_missing
    ),
    OpeningBalanceSourceIdentityDisposition.native_after_handoff: (
        PrepaidFundingQuarantineReason.native_after_handoff_opening_missing
    ),
}


class PrepaidActivationFundingError(DomainError):
    """Stable failure at the prepaid activation funding boundary."""


class PrepaidActivationFundingQuarantinedError(
    PrepaidActivationFundingError, ValueError
):
    """Prepaid activation refused because the account would be quarantined.

    It is also a ``ValueError`` so every existing lifecycle adapter that maps
    activation refusals (billing approval, provisioning gates) surfaces this
    actionable message instead of a generic failure.
    """


@dataclass(frozen=True, slots=True)
class PrepaidActivationFundingOverrideView:
    """Read model of one active override."""

    override_id: UUID
    granted_by: str
    granted_by_system_user_id: UUID
    granted_at: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class PrepaidFundingQuarantineAssessment:
    """Whether prepaid service for this account would be funding-quarantined."""

    account_id: UUID
    currency: str
    guard_active: bool
    funding_incomplete: bool
    reason: PrepaidFundingQuarantineReason | None
    runbook: PrepaidFundingRemediationRunbook | None
    splynx_linked: bool
    created_before_handoff: bool
    override: PrepaidActivationFundingOverrideView | None

    @property
    def quarantined(self) -> bool:
        """True when the guard applies and the account lacks funding authority."""

        return self.guard_active and self.funding_incomplete

    @property
    def admitted(self) -> bool:
        return not self.quarantined or self.override is not None

    @property
    def reason_summary(self) -> str | None:
        return _REASON_SUMMARY[self.reason] if self.reason is not None else None

    def refusal_message(self) -> str:
        runbook = self.runbook.value if self.runbook is not None else ""
        return (
            "Prepaid activation refused: this account would enter the prepaid "
            "funding quarantine and be excluded from every prepaid suspension "
            f"and restoration ({self.reason_summary}). Capture its reviewed "
            f"opening first ({runbook}), or have a user with "
            f"{OVERRIDE_PERMISSION} record an audited activation override."
        )


@dataclass(frozen=True, slots=True)
class GrantPrepaidActivationFundingOverrideCommand:
    """Record one staff decision to admit prepaid activation for an account.

    ``permission_granted`` is checked by the adapter against
    ``OVERRIDE_PERMISSION`` for the same principal named by
    ``actor_system_user_id``; the owner refuses when it is false.
    """

    context: CommandContext
    account_id: UUID
    actor_system_user_id: UUID
    permission_granted: bool
    reason: str


@dataclass(frozen=True, slots=True)
class RevokePrepaidActivationFundingOverrideCommand:
    """Withdraw the account's active override."""

    context: CommandContext
    account_id: UUID
    actor_system_user_id: UUID
    permission_granted: bool
    reason: str


@dataclass(frozen=True, slots=True)
class PrepaidActivationFundingOverrideOutcome:
    override_id: UUID
    account_id: UUID
    replayed: bool


def _error(
    suffix: str,
    message: str,
    **details: object,
) -> PrepaidActivationFundingError:
    return PrepaidActivationFundingError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=details,
        retryable=False,
    )


def _stored_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def customer_subledger_authority_active(db: Session) -> bool:
    return db.scalar(select(CustomerSubledgerAuthorityCutover.id).limit(1)) is not None


def _active_override(
    db: Session,
    account_id: UUID,
    *,
    lock: bool = False,
) -> PrepaidActivationFundingOverride | None:
    statement = select(PrepaidActivationFundingOverride).where(
        PrepaidActivationFundingOverride.account_id == account_id,
        PrepaidActivationFundingOverride.revoked_at.is_(None),
    )
    if lock:
        statement = statement.with_for_update()
    return db.scalars(statement).one_or_none()


def _override_view(
    row: PrepaidActivationFundingOverride | None,
) -> PrepaidActivationFundingOverrideView | None:
    if row is None:
        return None
    return PrepaidActivationFundingOverrideView(
        override_id=row.id,
        granted_by=row.granted_by,
        granted_by_system_user_id=row.granted_by_system_user_id,
        granted_at=_stored_utc(row.granted_at),
        reason=row.reason,
    )


def _classify_reason(
    db: Session,
    account: Subscriber,
) -> PrepaidFundingQuarantineReason:
    """Map the canonical carried-source classification to a quarantine reason."""

    try:
        snapshot = classify_opening_balance_source_identities(
            db,
            OpeningBalanceSourceIdentityQuery(
                account_ids=(account.id,),
                native_after=LEGACY_FINANCIAL_HANDOFF_AT,
                position_at=max(
                    datetime.now(UTC),
                    _stored_utc(account.created_at),
                ),
            ),
        )
    except OpeningBalanceHistoryError:
        return PrepaidFundingQuarantineReason.source_identity_unclassifiable
    return _DISPOSITION_REASON[snapshot.rows[0].disposition]


def assess_prepaid_funding_quarantine(
    db: Session,
    account_id: UUID | str,
    *,
    currency: str | None = None,
) -> PrepaidFundingQuarantineAssessment:
    """Read-only: would prepaid service for this account be quarantined?"""

    try:
        account_uuid = coerce_uuid(account_id)
    except (TypeError, ValueError) as exc:
        raise _error(
            "account_not_found",
            "The requested customer account does not exist.",
            account_id=str(account_id),
        ) from exc
    account = db.get(Subscriber, account_uuid)
    if account is None:
        raise _error(
            "account_not_found",
            "The requested customer account does not exist.",
            account_id=str(account_uuid),
        )
    unit = currency or default_prepaid_funding_currency(db)
    guard_active = customer_subledger_authority_active(db)
    funding_incomplete = account_uuid in prepaid_funding_incomplete_source_account_ids(
        db, [account_uuid], currency=unit
    )
    created_before_handoff = (
        _stored_utc(account.created_at) <= LEGACY_FINANCIAL_HANDOFF_AT
    )
    reason: PrepaidFundingQuarantineReason | None = None
    runbook: PrepaidFundingRemediationRunbook | None = None
    override = None
    if guard_active and funding_incomplete:
        reason = _classify_reason(db, account)
        runbook = _REASON_RUNBOOK[reason]
        override = _override_view(_active_override(db, account_uuid))
    return PrepaidFundingQuarantineAssessment(
        account_id=account_uuid,
        currency=unit,
        guard_active=guard_active,
        funding_incomplete=funding_incomplete,
        reason=reason,
        runbook=runbook,
        splynx_linked=account.splynx_customer_id is not None,
        created_before_handoff=created_before_handoff,
        override=override,
    )


def account_has_prepaid_exposure(db: Session, account_id: UUID | str) -> bool:
    """True when the account bills prepaid or holds a live prepaid service."""

    account_uuid = coerce_uuid(account_id)
    account = db.get(Subscriber, account_uuid)
    if account is None:
        return False
    if account.billing_mode == BillingMode.prepaid:
        return True
    return (
        db.scalar(
            select(func.count(Subscription.id)).where(
                Subscription.subscriber_id == account_uuid,
                Subscription.billing_mode == BillingMode.prepaid,
                Subscription.status.in_(COLLECTIBLE_SERVICE_STATUSES),
            )
        )
        or 0
    ) > 0


def require_prepaid_activation_funding_admitted(
    db: Session,
    *,
    account_id: UUID | str,
    entry_point: PrepaidActivationEntryPoint,
    subscription_id: UUID | str | None = None,
) -> PrepaidFundingQuarantineAssessment:
    """Fail closed before prepaid service starts for a quarantined account.

    Participant helper: it reads, and when an override admits the activation it
    stages an audit row in the caller's transaction (flush-only, no commit).
    """

    assessment = assess_prepaid_funding_quarantine(db, account_id)
    if not assessment.quarantined:
        return assessment
    details: dict[str, object] = {
        "account_id": str(assessment.account_id),
        "entry_point": entry_point.value,
        "reason": assessment.reason.value if assessment.reason else None,
        "runbook": assessment.runbook.value if assessment.runbook else None,
        "subscription_id": str(subscription_id) if subscription_id else None,
    }
    if assessment.override is None:
        logger.warning(
            "prepaid_activation_refused_funding_quarantine",
            extra={"event": "prepaid_activation_refused_funding_quarantine", **details},
        )
        raise PrepaidActivationFundingQuarantinedError(
            code=f"{OWNER}.funding_quarantined",
            message=assessment.refusal_message(),
            details=details,
            retryable=False,
        )
    override = assessment.override
    audit_service.audit_events.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.system,
            action=_ADMITTED_AUDIT_ACTION,
            entity_type="subscriber",
            entity_id=str(assessment.account_id),
            status_code=200,
            is_success=True,
            metadata_={
                **details,
                "override_id": str(override.override_id),
                "override_granted_by_system_user_id": str(
                    override.granted_by_system_user_id
                ),
            },
        ),
    )
    logger.warning(
        "prepaid_activation_admitted_by_funding_override",
        extra={
            "event": "prepaid_activation_admitted_by_funding_override",
            "override_id": str(override.override_id),
            **details,
        },
    )
    return assessment


def _definition(name: str) -> OwnerCommandDefinition:
    return OwnerCommandDefinition(owner=OWNER, concern=CONCERN, name=name)


def _execute(
    db: Session,
    *,
    context: CommandContext,
    name: str,
    operation: Callable[[], ResultT],
) -> ResultT:
    return execute_owner_command(
        db,
        definition=_definition(name),
        context=context,
        operation=operation,
    )


def _validate_decision(
    db: Session,
    *,
    context: CommandContext,
    permission_granted: bool,
    actor_system_user_id: UUID,
    reason: str,
) -> tuple[str, str, SystemUser]:
    if context.scope != OVERRIDE_PERMISSION:
        raise _error("invalid_scope", "The override command scope is invalid.")
    if not permission_granted:
        raise _error(
            "permission_denied",
            f"The {OVERRIDE_PERMISSION} permission is required.",
        )
    key = str(context.idempotency_key or "").strip()
    if not key:
        raise _error("missing_idempotency_key", "An idempotency key is required.")
    if len(key) > 160:
        raise _error("invalid_reason", "The idempotency key is too long.")
    normalized = " ".join(str(reason or "").split())
    if len(normalized) < MIN_OVERRIDE_REASON_LENGTH:
        raise _error(
            "invalid_reason",
            "Explain why prepaid service must start before the opening review "
            f"(at least {MIN_OVERRIDE_REASON_LENGTH} characters).",
            field="reason",
        )
    if len(normalized) > MAX_OVERRIDE_REASON_LENGTH:
        raise _error(
            "invalid_reason",
            f"The reason must be at most {MAX_OVERRIDE_REASON_LENGTH} characters.",
            field="reason",
        )
    actor = db.scalars(
        select(SystemUser).where(SystemUser.id == actor_system_user_id)
    ).one_or_none()
    if actor is None or not actor.is_active:
        raise _error(
            "actor_unavailable",
            "The override must be recorded by an active staff user.",
        )
    return key, normalized, actor


def _lock_account(db: Session, account_id: UUID) -> Subscriber:
    account = db.scalars(
        select(Subscriber).where(Subscriber.id == account_id).with_for_update()
    ).one_or_none()
    if account is None:
        raise _error(
            "account_not_found",
            "The requested customer account does not exist.",
            account_id=str(account_id),
        )
    return account


def _grant(
    db: Session,
    command: GrantPrepaidActivationFundingOverrideCommand,
) -> PrepaidActivationFundingOverrideOutcome:
    key, reason, _actor = _validate_decision(
        db,
        context=command.context,
        permission_granted=command.permission_granted,
        actor_system_user_id=command.actor_system_user_id,
        reason=command.reason,
    )
    replay = db.scalars(
        select(PrepaidActivationFundingOverride).where(
            PrepaidActivationFundingOverride.idempotency_key == key
        )
    ).one_or_none()
    if replay is not None:
        if (
            replay.account_id != command.account_id
            or replay.granted_by_system_user_id != command.actor_system_user_id
            or replay.reason != reason
        ):
            raise _error(
                "idempotency_conflict",
                "The idempotency key was already used for a different override.",
            )
        return PrepaidActivationFundingOverrideOutcome(
            override_id=replay.id, account_id=replay.account_id, replayed=True
        )
    account = _lock_account(db, command.account_id)
    assessment = assess_prepaid_funding_quarantine(db, account.id)
    if not assessment.quarantined or assessment.reason is None:
        raise _error(
            "not_quarantined",
            "This account is not funding-quarantined; no override is needed.",
            account_id=str(account.id),
        )
    if _active_override(db, account.id, lock=True) is not None:
        raise _error(
            "override_already_active",
            "This account already has an active prepaid activation override.",
            account_id=str(account.id),
        )
    assert assessment.runbook is not None
    granted_at = datetime.now(UTC)
    row = PrepaidActivationFundingOverride(
        account_id=account.id,
        currency=assessment.currency,
        quarantine_reason=assessment.reason.value,
        remediation_runbook=assessment.runbook.value,
        reason=reason,
        granted_by=command.context.actor,
        granted_by_system_user_id=command.actor_system_user_id,
        granted_at=granted_at,
        command_id=command.context.command_id,
        correlation_id=command.context.correlation_id,
        idempotency_key=key,
    )
    db.add(row)
    db.flush()
    metadata: dict[str, object] = {
        "override_id": str(row.id),
        "account_id": str(account.id),
        "currency": row.currency,
        "quarantine_reason": row.quarantine_reason,
        "remediation_runbook": row.remediation_runbook,
        "reason": reason,
        "command_id": str(command.context.command_id),
    }
    audit_service.audit_events.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.user,
            actor_id=str(command.actor_system_user_id),
            action=_GRANT_AUDIT_ACTION,
            entity_type="subscriber",
            entity_id=str(account.id),
            status_code=200,
            is_success=True,
            request_id=str(command.context.correlation_id),
            metadata_=metadata,
        ),
    )
    emit_event(
        db,
        EventType.prepaid_activation_funding_override_granted,
        {
            "account_id": str(account.id),
            "override_id": str(row.id),
            "quarantine_reason": row.quarantine_reason,
        },
        actor=command.context.actor,
        subscriber_id=account.id,
    )
    db.flush()
    return PrepaidActivationFundingOverrideOutcome(
        override_id=row.id, account_id=account.id, replayed=False
    )


def grant_prepaid_activation_funding_override(
    db: Session,
    command: GrantPrepaidActivationFundingOverrideCommand,
) -> PrepaidActivationFundingOverrideOutcome:
    """Persist one audited override decision in the owner transaction."""

    return _execute(
        db,
        context=command.context,
        name="grant_prepaid_activation_funding_override",
        operation=lambda: _grant(db, command),
    )


def _revoke(
    db: Session,
    command: RevokePrepaidActivationFundingOverrideCommand,
) -> PrepaidActivationFundingOverrideOutcome:
    _key, reason, _actor = _validate_decision(
        db,
        context=command.context,
        permission_granted=command.permission_granted,
        actor_system_user_id=command.actor_system_user_id,
        reason=command.reason,
    )
    account = _lock_account(db, command.account_id)
    row = _active_override(db, account.id, lock=True)
    if row is None:
        raise _error(
            "override_not_found",
            "This account has no active prepaid activation override.",
            account_id=str(account.id),
        )
    row.revoked_at = datetime.now(UTC)
    row.revoked_by = command.context.actor
    row.revoked_by_system_user_id = command.actor_system_user_id
    row.revoke_reason = reason
    db.flush()
    audit_service.audit_events.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.user,
            actor_id=str(command.actor_system_user_id),
            action=_REVOKE_AUDIT_ACTION,
            entity_type="subscriber",
            entity_id=str(account.id),
            status_code=200,
            is_success=True,
            request_id=str(command.context.correlation_id),
            metadata_={
                "override_id": str(row.id),
                "account_id": str(account.id),
                "reason": reason,
                "command_id": str(command.context.command_id),
            },
        ),
    )
    emit_event(
        db,
        EventType.prepaid_activation_funding_override_revoked,
        {"account_id": str(account.id), "override_id": str(row.id)},
        actor=command.context.actor,
        subscriber_id=account.id,
    )
    db.flush()
    return PrepaidActivationFundingOverrideOutcome(
        override_id=row.id, account_id=account.id, replayed=False
    )


def revoke_prepaid_activation_funding_override(
    db: Session,
    command: RevokePrepaidActivationFundingOverrideCommand,
) -> PrepaidActivationFundingOverrideOutcome:
    """Withdraw the active override; later activations fail closed again."""

    return _execute(
        db,
        context=command.context,
        name="revoke_prepaid_activation_funding_override",
        operation=lambda: _revoke(db, command),
    )


__all__ = [
    "OVERRIDE_PERMISSION",
    "GrantPrepaidActivationFundingOverrideCommand",
    "PrepaidActivationEntryPoint",
    "PrepaidActivationFundingError",
    "PrepaidActivationFundingOverrideOutcome",
    "PrepaidActivationFundingOverrideView",
    "PrepaidActivationFundingQuarantinedError",
    "PrepaidFundingQuarantineAssessment",
    "PrepaidFundingQuarantineReason",
    "PrepaidFundingRemediationRunbook",
    "RevokePrepaidActivationFundingOverrideCommand",
    "account_has_prepaid_exposure",
    "assess_prepaid_funding_quarantine",
    "customer_subledger_authority_active",
    "grant_prepaid_activation_funding_override",
    "require_prepaid_activation_funding_admitted",
    "revoke_prepaid_activation_funding_override",
]
