"""Canonical preview and execution for subscription lifecycle batches."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.services.subscription_lifecycle import (
    SubscriptionAccessImpact,
    SubscriptionBillingImpact,
    SubscriptionCommandKind,
    SubscriptionCommandOutcomeStatus,
    SubscriptionEffectiveTiming,
    SubscriptionLifecycleCommand,
    SubscriptionLifecycleError,
    SubscriptionLifecycleState,
    preview_subscription_command,
)
from app.services.subscription_lifecycle_commands import execute_subscription_command

MAX_BATCH_SIZE = 200

_SUCCESS_STATUSES = frozenset(
    {
        SubscriptionCommandOutcomeStatus.applied,
        SubscriptionCommandOutcomeStatus.scheduled,
        SubscriptionCommandOutcomeStatus.skipped,
    }
)


class SubscriptionLifecycleBatchError(ValueError):
    pass


@dataclass(frozen=True)
class SubscriptionBatchPreviewItem:
    subscription_id: str
    eligible: bool
    eligibility_reasons: tuple[str, ...]
    expected_head: str | None = None
    effective_at: datetime | None = None
    current: SubscriptionLifecycleState | None = None
    proposed: SubscriptionLifecycleState | None = None
    billing_impact: SubscriptionBillingImpact | None = None
    access_impact: SubscriptionAccessImpact | None = None
    error_code: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class SubscriptionBatchPreview:
    kind: SubscriptionCommandKind
    items: tuple[SubscriptionBatchPreviewItem, ...]

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def eligible_count(self) -> int:
        return sum(item.eligible for item in self.items)

    @property
    def ineligible_count(self) -> int:
        return self.total - self.eligible_count

    @property
    def reviewed_heads(self) -> dict[str, str]:
        return {
            item.subscription_id: item.expected_head
            for item in self.items
            if item.expected_head is not None
        }


@dataclass(frozen=True)
class SubscriptionBatchOutcomeItem:
    subscription_id: str
    status: SubscriptionCommandOutcomeStatus
    message: str
    previous_head: str | None = None
    current_head: str | None = None
    artifact_ids: tuple[str, ...] = ()
    error_code: str | None = None
    replayed: bool = False


@dataclass(frozen=True)
class SubscriptionBatchOutcome:
    kind: SubscriptionCommandKind
    items: tuple[SubscriptionBatchOutcomeItem, ...]

    @property
    def total(self) -> int:
        return len(self.items)

    def count(self, status: SubscriptionCommandOutcomeStatus) -> int:
        return sum(item.status == status for item in self.items)

    @property
    def succeeded(self) -> int:
        return sum(item.status in _SUCCESS_STATUSES for item in self.items)

    @property
    def status(self) -> str:
        if self.succeeded == self.total:
            return "completed"
        if self.succeeded:
            return "partial"
        if self.count(SubscriptionCommandOutcomeStatus.failed):
            return "failed"
        return "rejected"


def preview_subscription_batch(
    db: Session,
    subscription_ids: str | Iterable[str],
    *,
    kind: SubscriptionCommandKind,
    source: str,
    target_offer_id: str | None = None,
    effective_timing: SubscriptionEffectiveTiming = (
        SubscriptionEffectiveTiming.immediate
    ),
    effective_at: datetime | None = None,
    reason: str | None = None,
) -> SubscriptionBatchPreview:
    """Preview each selected subscription without mutating any item."""
    items: list[SubscriptionBatchPreviewItem] = []
    for subscription_id in normalize_subscription_ids(subscription_ids):
        try:
            command = _command(
                subscription_id,
                kind=kind,
                source=source,
                target_offer_id=target_offer_id,
                effective_timing=effective_timing,
                effective_at=effective_at,
                reason=reason,
            )
            preview = preview_subscription_command(db, command)
        except (SubscriptionLifecycleError, ValueError) as exc:
            items.append(
                SubscriptionBatchPreviewItem(
                    subscription_id=subscription_id,
                    eligible=False,
                    eligibility_reasons=("invalid_lifecycle_command",),
                    error_code="invalid_lifecycle_command",
                    message=str(exc),
                )
            )
            continue
        items.append(
            SubscriptionBatchPreviewItem(
                subscription_id=subscription_id,
                eligible=preview.eligible,
                eligibility_reasons=preview.eligibility_reasons,
                expected_head=preview.current.head,
                effective_at=preview.effective_at,
                current=preview.current.state,
                proposed=preview.proposed,
                billing_impact=preview.billing_impact,
                access_impact=preview.access_impact,
            )
        )
    return SubscriptionBatchPreview(kind=kind, items=tuple(items))


def execute_subscription_batch(
    db: Session,
    subscription_ids: str | Iterable[str],
    *,
    kind: SubscriptionCommandKind,
    source: str,
    actor_id: str | None,
    target_offer_id: str | None = None,
    effective_timing: SubscriptionEffectiveTiming = (
        SubscriptionEffectiveTiming.immediate
    ),
    effective_at: datetime | None = None,
    reason: str | None = None,
    reviewed_heads: Mapping[str, str] | None = None,
    idempotency_key: str | None = None,
    require_reviewed_heads: bool = True,
) -> SubscriptionBatchOutcome:
    """Execute every selected item independently through the canonical owner."""
    heads = reviewed_heads or {}
    items: list[SubscriptionBatchOutcomeItem] = []
    for subscription_id in normalize_subscription_ids(subscription_ids):
        expected_head = heads.get(subscription_id)
        if require_reviewed_heads and not expected_head:
            items.append(
                SubscriptionBatchOutcomeItem(
                    subscription_id=subscription_id,
                    status=SubscriptionCommandOutcomeStatus.rejected,
                    message="The subscription was not included in the reviewed batch",
                    error_code="missing_reviewed_head",
                )
            )
            continue
        try:
            command = _command(
                subscription_id,
                kind=kind,
                source=source,
                target_offer_id=target_offer_id,
                effective_timing=effective_timing,
                effective_at=effective_at,
                reason=reason,
                expected_head=expected_head,
                idempotency_key=idempotency_key,
            )
            outcome = execute_subscription_command(
                db,
                command,
                actor_id=actor_id,
                actor_type=(AuditActorType.user if actor_id else AuditActorType.system),
            )
        except (SubscriptionLifecycleError, ValueError) as exc:
            db.rollback()
            items.append(
                SubscriptionBatchOutcomeItem(
                    subscription_id=subscription_id,
                    status=SubscriptionCommandOutcomeStatus.rejected,
                    message=str(exc),
                    previous_head=expected_head,
                    error_code="invalid_lifecycle_command",
                )
            )
            continue
        except Exception as exc:
            db.rollback()
            items.append(
                SubscriptionBatchOutcomeItem(
                    subscription_id=subscription_id,
                    status=SubscriptionCommandOutcomeStatus.failed,
                    message=str(exc) or type(exc).__name__,
                    previous_head=expected_head,
                    error_code="command_execution_failed",
                )
            )
            continue
        items.append(
            SubscriptionBatchOutcomeItem(
                subscription_id=subscription_id,
                status=outcome.status,
                message=outcome.message,
                previous_head=outcome.previous_head,
                current_head=outcome.current_head,
                artifact_ids=outcome.artifact_ids,
                error_code=outcome.error_code,
                replayed=outcome.replayed,
            )
        )
    return SubscriptionBatchOutcome(kind=kind, items=tuple(items))


def normalize_subscription_ids(
    subscription_ids: str | Iterable[str],
) -> tuple[str, ...]:
    values = (
        subscription_ids.split(",")
        if isinstance(subscription_ids, str)
        else subscription_ids
    )
    normalized = tuple(dict.fromkeys(str(value).strip() for value in values if value))
    normalized = tuple(value for value in normalized if value)
    if not normalized:
        raise SubscriptionLifecycleBatchError("At least one subscription is required")
    if len(normalized) > MAX_BATCH_SIZE:
        raise SubscriptionLifecycleBatchError(
            f"A lifecycle batch cannot exceed {MAX_BATCH_SIZE} subscriptions"
        )
    return normalized


def _command(
    subscription_id: str,
    *,
    kind: SubscriptionCommandKind,
    source: str,
    target_offer_id: str | None,
    effective_timing: SubscriptionEffectiveTiming,
    effective_at: datetime | None,
    reason: str | None,
    expected_head: str | None = None,
    idempotency_key: str | None = None,
) -> SubscriptionLifecycleCommand:
    return SubscriptionLifecycleCommand(
        subscription_id=subscription_id,
        kind=kind,
        source=source,
        effective_timing=effective_timing,
        effective_at=effective_at,
        target_offer_id=target_offer_id,
        reason=reason,
        expected_head=expected_head,
        idempotency_key=idempotency_key,
    )


__all__ = [
    "MAX_BATCH_SIZE",
    "SubscriptionBatchOutcome",
    "SubscriptionBatchOutcomeItem",
    "SubscriptionBatchPreview",
    "SubscriptionBatchPreviewItem",
    "SubscriptionLifecycleBatchError",
    "execute_subscription_batch",
    "normalize_subscription_ids",
    "preview_subscription_batch",
]
