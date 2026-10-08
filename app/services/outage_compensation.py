"""Owner for exact-second compensation from finalized customer outages."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TypedDict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import ServiceEntitlement, ServiceEntitlementStatus
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.network_monitoring import CustomerOutageInterval
from app.models.service_period_purchase import (
    OutageCompensationDecision,
    OutageCompensationDecisionInterval,
    OutageCompensationDecisionStatus,
)
from app.models.subscription_change import (
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.models.subscription_lifecycle_schedule import (
    SubscriptionLifecycleSchedule,
    SubscriptionLifecycleScheduleStatus,
)
from app.schemas.audit import AuditEventCreate
from app.services.account_lifecycle import (
    BillingAnchorProjectionCommand,
    BillingAnchorProjectionSource,
    stage_subscription_billing_anchor,
)
from app.services.audit import AuditEvents
from app.services.billing._common import lock_account
from app.services.domain_errors import DomainError
from app.services.outage_interval_algebra import (
    TimeInterval,
    intersect_intervals,
    intersect_seconds,
    interval_seconds,
    merge_intervals,
    subtract_intervals,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.service_period_policy import (
    OutageCompensationPolicy,
    resolve_outage_compensation_policy,
)

_OWNER = "financial.outage_compensation"
_POLICY_VERSION = 3
OUTAGE_REPAIR_SCOPE = "billing:prepaid_reconciliation:repair"
OUTAGE_APPROVAL_SCOPE = "billing:outage_compensation:approve"
_APPROVE_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern="reviewed outage grant approval",
    name="approve_outage_compensation",
)
_REVIEW_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern="reviewed outage compensation recovery",
    name="review_outage_compensation",
)
_APPLY_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern="finalized outage service-period compensation",
    name="apply_outage_compensation",
)
_CONSUME_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern="finalized outage service-period compensation",
    name="consume_outage_compensation_event",
)


class OutageCompensationError(DomainError, ValueError):
    """Stable fail-closed outage compensation error."""


def _error(suffix: str, message: str, **details: object) -> OutageCompensationError:
    return OutageCompensationError(
        code=f"{_OWNER}.{suffix}", message=message, details=details
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class CompensationEvidenceSnapshot(TypedDict):
    threshold_seconds: int
    posting_policy: str
    unresolved_time_credit_ids: list[str]
    review_decision_id: str | None
    duration_unit: str
    funding_cap: str
    planned_maintenance: str
    evaluated_at: str
    source_interval_ids: list[str]
    funded_entitlement_ids: list[str]
    compensated_ranges: list[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class OutageCompensationPreview:
    account_id: UUID
    subscription_id: UUID
    interval_ids: tuple[UUID, ...]
    status: OutageCompensationDecisionStatus
    threshold_seconds: int
    eligible_seconds: int
    funded_overlap_seconds: int
    tail_before: datetime | None
    tail_after: datetime | None
    fingerprint: str
    policy_snapshot: CompensationEvidenceSnapshot


@dataclass(frozen=True, slots=True)
class ApplyOutageCompensationCommand:
    subscription_id: UUID
    expected_fingerprint: str
    idempotency_key: str
    effective_at: datetime
    context: CommandContext
    review_decision_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class OutageCompensationResult:
    decision_id: UUID
    status: OutageCompensationDecisionStatus
    entitlement_id: UUID | None
    compensated_seconds: int
    tail_after: datetime | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class ReviewOutageCompensationCommand:
    subscription_id: UUID
    review_decision_id: UUID
    expected_fingerprint: str
    effective_at: datetime
    permission_granted: bool
    actor_system_user_id: UUID


def review_outage_compensation(
    db: Session,
    command: ReviewOutageCompensationCommand,
    *,
    context: CommandContext,
) -> OutageCompensationResult:
    def operation() -> OutageCompensationResult:
        if not command.permission_granted or context.scope != OUTAGE_REPAIR_SCOPE:
            raise _error(
                "repair_permission_required",
                "Prepaid reconciliation permission is required.",
            )
        if not context.idempotency_key or not context.reason.strip():
            raise _error(
                "idempotency_required", "A recovery key and reason are required."
            )
        result = _stage_outage_compensation(
            db,
            ApplyOutageCompensationCommand(
                subscription_id=command.subscription_id,
                expected_fingerprint=command.expected_fingerprint,
                idempotency_key=context.idempotency_key,
                effective_at=command.effective_at,
                context=context,
                review_decision_id=command.review_decision_id,
            ),
        )
        if not result.replayed:
            AuditEvents.stage(
                db,
                AuditEventCreate(
                    actor_type=AuditActorType.user,
                    actor_id=str(command.actor_system_user_id),
                    actor_label=context.actor,
                    action="outage_compensation.review_recovery",
                    entity_type="outage_compensation_decision",
                    entity_id=str(result.decision_id),
                    metadata_={
                        "review_decision_id": str(command.review_decision_id),
                        "preview_fingerprint": command.expected_fingerprint,
                        "reason": context.reason,
                        "status": result.status.value,
                    },
                ),
            )
        return result

    return execute_owner_command(
        db, definition=_REVIEW_COMMAND, context=context, operation=operation
    )


def _policy(db: Session) -> OutageCompensationPolicy:
    try:
        return resolve_outage_compensation_policy(db)
    except (TypeError, ValueError) as exc:
        raise _error("configuration_invalid", "Outage threshold is invalid.") from exc


def _pending_cluster(
    db: Session, subscription_id: UUID, *, review_decision_id: UUID | None = None
) -> tuple[CustomerOutageInterval, ...]:
    consumed = set(
        db.scalars(
            select(OutageCompensationDecisionInterval.customer_outage_interval_id)
            .join(OutageCompensationDecision)
            .where(OutageCompensationDecision.subscription_id == subscription_id)
        ).all()
    )
    rows = list(
        db.scalars(
            select(CustomerOutageInterval)
            .where(
                CustomerOutageInterval.subscription_id == subscription_id,
                CustomerOutageInterval.state == "confirmed_unavailable",
                CustomerOutageInterval.ended_at.is_not(None),
                CustomerOutageInterval.finalized_at.is_not(None),
            )
            .order_by(CustomerOutageInterval.started_at, CustomerOutageInterval.id)
        ).all()
    )
    if review_decision_id is not None:
        review = db.get(OutageCompensationDecision, review_decision_id)
        if (
            review is None
            or review.subscription_id != subscription_id
            or review.status
            not in {
                OutageCompensationDecisionStatus.review_required,
                OutageCompensationDecisionStatus.awaiting_approval,
            }
            or review.resolved_by_decision_id is not None
        ):
            raise _error(
                "review_invalid",
                "An unresolved review decision for this service is required.",
            )
        review_ids = {item.customer_outage_interval_id for item in review.intervals}
        pending = [row for row in rows if row.id in review_ids]
    else:
        pending = [row for row in rows if row.id not in consumed]
    if not pending:
        return ()
    seed = pending[0]
    if seed.exclusion_candidate is not None:
        return (seed,)
    # Include already decided evidence: a later source can bridge a previous
    # below-threshold component or overlap seconds already compensated.
    cluster: list[CustomerOutageInterval] = []
    cluster_end: datetime | None = None
    for row in rows:
        if row.exclusion_candidate is not None:
            continue
        assert row.ended_at is not None
        if cluster_end is not None and _utc(row.started_at) > cluster_end:
            if seed in cluster:
                return tuple(cluster)
            cluster = []
        cluster.append(row)
        cluster_end = max(_utc(item.ended_at) for item in cluster if item.ended_at)
    return tuple(cluster)


def preview_outage_compensation(
    db: Session,
    *,
    subscription_id: UUID,
    effective_at: datetime,
    review_decision_id: UUID | None = None,
) -> OutageCompensationPreview:
    policy = _policy(db)
    if not policy.enabled:
        raise _error("feature_disabled", "Outage compensation is not enabled.")
    subscription = db.get(Subscription, subscription_id)
    if subscription is None:
        raise _error("subscription_not_found", "Subscription was not found.")
    rows = _pending_cluster(db, subscription_id, review_decision_id=review_decision_id)
    if not rows:
        raise _error("no_finalized_outage", "No finalized outage awaits a decision.")
    threshold = policy.minimum_seconds
    eligible = merge_intervals(
        [
            TimeInterval(row.started_at, row.ended_at)
            for row in rows
            if row.ended_at is not None and row.exclusion_candidate is None
        ]
    )
    eligible_seconds = interval_seconds(eligible)
    funded_rows = list(
        db.scalars(
            select(ServiceEntitlement)
            .where(
                ServiceEntitlement.subscription_id == subscription_id,
                ServiceEntitlement.account_id == subscription.subscriber_id,
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
            )
            .order_by(ServiceEntitlement.starts_at, ServiceEntitlement.ends_at)
        ).all()
    )
    funded = merge_intervals(
        [TimeInterval(row.starts_at, row.ends_at) for row in funded_rows]
    )
    previous = list(
        db.scalars(
            select(OutageCompensationDecision).where(
                OutageCompensationDecision.subscription_id == subscription_id,
                OutageCompensationDecision.status
                == OutageCompensationDecisionStatus.compensated,
            )
        ).all()
    )
    from app.services.compensated_service_time import (
        TimeCreditQuery,
        resolve_compensated_service_time,
    )

    history = resolve_compensated_service_time(db, TimeCreditQuery(subscription_id))
    credited = history.credited
    newly_funded = subtract_intervals(intersect_intervals(eligible, funded), credited)
    funded_overlap = interval_seconds(newly_funded)
    tail_before = max((item.ends_at for item in funded), default=None)
    status = OutageCompensationDecisionStatus.compensated
    if not eligible:
        status = OutageCompensationDecisionStatus.excluded
    elif eligible_seconds < threshold:
        status = OutageCompensationDecisionStatus.below_threshold
    elif funded_overlap <= 0 or tail_before is None:
        status = OutageCompensationDecisionStatus.no_funded_overlap
    elif (
        subscription.next_billing_at is not None
        and _utc(subscription.next_billing_at) > tail_before
    ):
        status = OutageCompensationDecisionStatus.review_required
    if (
        subscription.billing_mode is not BillingMode.prepaid
        or subscription.status is not SubscriptionStatus.active
        or (tail_before is not None and tail_before <= _utc(effective_at))
        or not any(
            item.starts_at <= _utc(effective_at) < item.ends_at for item in funded
        )
        or any(
            "compensated_ranges" not in (item.policy_snapshot or {})
            for item in previous
        )
        or any(
            row.quality != "exact" for row in rows if row.exclusion_candidate is None
        )
        or any(
            row.ended_at is None
            or _utc(row.ended_at) > _utc(effective_at)
            or _utc(row.ended_at) <= _utc(row.started_at)
            for row in rows
        )
    ):
        status = OutageCompensationDecisionStatus.review_required
    if any(
        intersect_seconds(eligible, (item.interval,)) for item in history.unresolved
    ):
        status = OutageCompensationDecisionStatus.review_required
    tail_after = (
        tail_before + timedelta(seconds=funded_overlap)
        if status is OutageCompensationDecisionStatus.compensated
        and tail_before is not None
        else tail_before
    )
    if (
        subscription.end_at is not None
        and tail_after is not None
        and _utc(subscription.end_at) < tail_after
    ):
        status = OutageCompensationDecisionStatus.review_required
        tail_after = tail_before
    if tail_after is not None and (
        db.scalar(
            select(SubscriptionChangeRequest.id)
            .where(
                SubscriptionChangeRequest.subscription_id == subscription.id,
                SubscriptionChangeRequest.is_active.is_(True),
                SubscriptionChangeRequest.status.in_(
                    [
                        SubscriptionChangeStatus.pending,
                        SubscriptionChangeStatus.approved,
                    ]
                ),
                SubscriptionChangeRequest.effective_date <= tail_after.date(),
            )
            .limit(1)
        )
        is not None
        or db.scalar(
            select(SubscriptionLifecycleSchedule.id)
            .where(
                SubscriptionLifecycleSchedule.subscription_id == subscription.id,
                SubscriptionLifecycleSchedule.status.in_(
                    [
                        SubscriptionLifecycleScheduleStatus.pending,
                        SubscriptionLifecycleScheduleStatus.processing,
                    ]
                ),
                SubscriptionLifecycleSchedule.effective_timing != "next_cycle",
                SubscriptionLifecycleSchedule.effective_at < tail_after,
            )
            .limit(1)
        )
        is not None
    ):
        status = OutageCompensationDecisionStatus.review_required
        tail_after = tail_before
    consumed_ids = set(
        db.scalars(
            select(
                OutageCompensationDecisionInterval.customer_outage_interval_id
            ).where(
                OutageCompensationDecisionInterval.customer_outage_interval_id.in_(
                    [row.id for row in rows]
                )
            )
        ).all()
    )
    policy_snapshot: CompensationEvidenceSnapshot = {
        "threshold_seconds": threshold,
        "posting_policy": "staff_approval_required",
        "unresolved_time_credit_ids": [
            str(item.source_id)
            for item in history.unresolved
            if intersect_seconds(eligible, (item.interval,))
        ],
        "review_decision_id": str(review_decision_id) if review_decision_id else None,
        "duration_unit": "exact_seconds",
        "funding_cap": "outage_intersection_with_active_entitlements",
        "planned_maintenance": "exclude_explicit_candidates",
        "evaluated_at": _utc(effective_at).isoformat(),
        "source_interval_ids": [str(row.id) for row in rows],
        "funded_entitlement_ids": [
            str(row.id)
            for row in funded_rows
            if _utc(row.ends_at) == tail_before
            or intersect_seconds(
                eligible, (TimeInterval(_utc(row.starts_at), _utc(row.ends_at)),)
            )
            > 0
        ],
        "compensated_ranges": [
            (item.starts_at.isoformat(), item.ends_at.isoformat())
            for item in newly_funded
        ]
        if status is OutageCompensationDecisionStatus.compensated
        else [],
    }
    payload = {
        "subscription_id": str(subscription_id),
        "account_id": str(subscription.subscriber_id),
        "intervals": [
            {
                "id": str(row.id),
                "start": _utc(row.started_at).isoformat(),
                "end": _utc(row.ended_at).isoformat() if row.ended_at else None,
                "excluded": row.exclusion_candidate,
            }
            for row in rows
        ],
        "funded": [
            (
                str(row.id),
                _utc(row.starts_at).isoformat(),
                _utc(row.ends_at).isoformat(),
            )
            for row in funded_rows
        ],
        "status": status.value,
        "eligible_seconds": eligible_seconds,
        "funded_overlap_seconds": funded_overlap,
        "tail_before": tail_before.isoformat() if tail_before else None,
        "tail_after": tail_after.isoformat() if tail_after else None,
        "policy": {
            key: value
            for key, value in policy_snapshot.items()
            if key != "evaluated_at"
        },
        "policy_version": _POLICY_VERSION,
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return OutageCompensationPreview(
        account_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        interval_ids=tuple(row.id for row in rows if row.id not in consumed_ids),
        status=status,
        threshold_seconds=threshold,
        eligible_seconds=eligible_seconds,
        funded_overlap_seconds=funded_overlap,
        tail_before=tail_before,
        tail_after=tail_after,
        fingerprint=fingerprint,
        policy_snapshot=policy_snapshot,
    )


def apply_outage_compensation(
    db: Session, command: ApplyOutageCompensationCommand
) -> OutageCompensationResult:
    return execute_owner_command(
        db,
        definition=_APPLY_COMMAND,
        context=command.context,
        operation=lambda: _stage_outage_compensation(db, command),
    )


def _stage_outage_compensation(
    db: Session,
    command: ApplyOutageCompensationCommand,
    *,
    approved_by: UUID | None = None,
) -> OutageCompensationResult:
    key = command.idempotency_key.strip()
    if not key:
        raise _error("idempotency_required", "An idempotency key is required.")
    subscription = db.get(Subscription, command.subscription_id)
    if subscription is None:
        raise _error("subscription_not_found", "Subscription was not found.")
    lock_account(db, str(subscription.subscriber_id))
    db.refresh(subscription, with_for_update=True)
    existing = db.scalar(
        select(OutageCompensationDecision).where(
            OutageCompensationDecision.idempotency_key == key
        )
    )
    if existing is not None:
        if existing.preview_fingerprint != command.expected_fingerprint:
            raise _error("idempotency_conflict", "Decision key names another preview.")
        return OutageCompensationResult(
            decision_id=existing.id,
            status=existing.status,
            entitlement_id=existing.entitlement_id,
            compensated_seconds=(
                existing.funded_overlap_seconds
                if existing.status is OutageCompensationDecisionStatus.compensated
                else 0
            ),
            tail_after=existing.tail_after,
            replayed=True,
        )
    preview = preview_outage_compensation(
        db,
        subscription_id=subscription.id,
        effective_at=max(_utc(command.effective_at), datetime.now(UTC)),
        review_decision_id=command.review_decision_id,
    )
    if preview.fingerprint != command.expected_fingerprint:
        raise _error("stale_preview", "Outage evidence changed before application.")
    decision = OutageCompensationDecision(
        account_id=preview.account_id,
        subscription_id=preview.subscription_id,
        status=(
            OutageCompensationDecisionStatus.awaiting_approval
            if preview.status is OutageCompensationDecisionStatus.compensated
            and approved_by is None
            else preview.status
        ),
        approved_by=approved_by,
        approval_reason=command.context.reason if approved_by else None,
        approved_fingerprint=command.expected_fingerprint if approved_by else None,
        threshold_seconds=preview.threshold_seconds,
        eligible_seconds=preview.eligible_seconds,
        funded_overlap_seconds=preview.funded_overlap_seconds,
        tail_before=preview.tail_before,
        tail_after=preview.tail_after,
        policy_version=_POLICY_VERSION,
        policy_snapshot=preview.policy_snapshot,
        preview_fingerprint=preview.fingerprint,
        idempotency_key=key,
        created_by=command.context.actor,
        applied_at=_utc(command.effective_at) if approved_by else None,
    )
    db.add(decision)
    db.flush()
    rows = list(
        db.scalars(
            select(CustomerOutageInterval)
            .where(CustomerOutageInterval.id.in_(preview.interval_ids))
            .order_by(CustomerOutageInterval.started_at, CustomerOutageInterval.id)
        ).all()
    )
    eligible_seen: list[TimeInterval] = []
    for row in rows:
        assert row.ended_at is not None
        duration = int((_utc(row.ended_at) - _utc(row.started_at)).total_seconds())
        if row.exclusion_candidate is not None:
            included_seconds = 0
            excluded_seconds = duration
        else:
            before = interval_seconds(merge_intervals(eligible_seen))
            eligible_seen.append(TimeInterval(row.started_at, row.ended_at))
            after = interval_seconds(merge_intervals(eligible_seen))
            included_seconds = after - before
            excluded_seconds = duration - included_seconds
        db.add(
            OutageCompensationDecisionInterval(
                decision_id=decision.id,
                customer_outage_interval_id=row.id,
                incident_id=row.incident_id,
                started_at=row.started_at,
                ended_at=row.ended_at,
                included_seconds=included_seconds,
                excluded_seconds=excluded_seconds,
                exclusion_reason=row.exclusion_candidate,
            )
        )
    entitlement_id: UUID | None = None
    if (
        preview.status is OutageCompensationDecisionStatus.compensated
        and approved_by is not None
    ):
        assert preview.tail_before is not None and preview.tail_after is not None
        tail_currency = (
            db.scalar(
                select(ServiceEntitlement.currency)
                .where(
                    ServiceEntitlement.subscription_id == subscription.id,
                    ServiceEntitlement.status == ServiceEntitlementStatus.active,
                    ServiceEntitlement.ends_at == preview.tail_before,
                )
                .order_by(ServiceEntitlement.created_at.desc())
            )
            or "NGN"
        )
        entitlement = ServiceEntitlement(
            account_id=subscription.subscriber_id,
            subscription_id=subscription.id,
            source_outage_compensation_id=decision.id,
            starts_at=preview.tail_before,
            ends_at=preview.tail_after,
            amount_funded=Decimal("0.00"),
            currency=tail_currency,
            status=ServiceEntitlementStatus.active,
            metadata_={
                "source": "outage_compensation",
                "decision_id": str(decision.id),
                "exact_seconds": preview.funded_overlap_seconds,
                "source_interval_ids": [str(item) for item in preview.interval_ids],
            },
        )
        db.add(entitlement)
        db.flush()
        entitlement_id = entitlement.id
        decision.entitlement_id = entitlement.id
        from app.services.compensated_service_time import (
            StageTimeCreditCommand,
            TimeCreditSource,
            stage_compensated_service_time,
        )

        ranges = tuple(
            TimeInterval(datetime.fromisoformat(start), datetime.fromisoformat(end))
            for start, end in preview.policy_snapshot["compensated_ranges"]
        )
        stage_compensated_service_time(
            db,
            StageTimeCreditCommand(
                subscription_id=subscription.id,
                source=TimeCreditSource.outage,
                source_id=decision.id,
                ranges=ranges,
                evidence_ref=f"outage-compensation:{decision.id}",
            ),
        )
        from app.services.subscription_lifecycle import resolve_subscription_lifecycle

        previous_head = resolve_subscription_lifecycle(db, str(subscription.id)).head
        stage_subscription_billing_anchor(
            db,
            subscription,
            BillingAnchorProjectionCommand(
                subscription_id=subscription.id,
                expected_previous=subscription.next_billing_at,
                target=preview.tail_after,
                source=BillingAnchorProjectionSource.outage_compensation,
                evidence_ref=f"outage-compensation:{decision.id}",
            ),
        )
        from app.services.subscription_lifecycle_schedules import (
            stage_rebase_funded_tail_termination_schedules,
        )

        stage_rebase_funded_tail_termination_schedules(
            db,
            subscription_id=subscription.id,
            previous_tail=preview.tail_before,
            extended_tail=preview.tail_after,
            evidence_ref=f"outage-compensation:{decision.id}",
            expected_previous_head=previous_head,
        )
    db.flush()
    if (
        command.review_decision_id is not None
        and decision.status is not OutageCompensationDecisionStatus.review_required
    ):
        original = db.get(OutageCompensationDecision, command.review_decision_id)
        assert original is not None
        original.resolved_by_decision_id = decision.id
        db.flush()
    from app.services.events import emit_event
    from app.services.events.types import EventType

    emit_event(
        db,
        EventType.outage_compensation_approved
        if approved_by
        else EventType.outage_compensation_proposed,
        {
            "schema_version": 1,
            "decision_id": str(decision.id),
            "status": decision.status.value,
            "preview_fingerprint": decision.preview_fingerprint,
            "seconds": decision.funded_overlap_seconds,
        },
        actor=command.context.actor,
        account_id=subscription.subscriber_id,
        subscription_id=subscription.id,
    )
    return OutageCompensationResult(
        decision_id=decision.id,
        status=decision.status,
        entitlement_id=entitlement_id,
        compensated_seconds=(
            decision.funded_overlap_seconds
            if decision.status is OutageCompensationDecisionStatus.compensated
            else 0
        ),
        tail_after=decision.tail_after,
        replayed=False,
    )


@dataclass(frozen=True, slots=True)
class ApproveOutageCompensationCommand:
    decision_id: UUID
    expected_fingerprint: str
    actor_system_user_id: UUID
    effective_at: datetime


def require_time_credit_staff(db: Session, principal_id: UUID, permission: str) -> None:
    from app.models.system_user import SystemUser
    from app.services.auth_dependencies import has_permission
    from app.services.system_user_assignments import system_user_role_names

    principal = db.get(SystemUser, principal_id)
    if (
        principal is None
        or not principal.is_active
        or not has_permission(
            {
                "principal_id": str(principal_id),
                "principal_type": "system_user",
                "roles": set(system_user_role_names(db, principal_id)),
            },
            db,
            permission,
        )
    ):
        raise _error(
            "approval_permission_required",
            "An active staff approver with the required permission is required.",
        )


def approve_outage_compensation(
    db: Session, command: ApproveOutageCompensationCommand, *, context: CommandContext
) -> OutageCompensationResult:
    def operation() -> OutageCompensationResult:
        if (
            context.scope != OUTAGE_APPROVAL_SCOPE
            or context.actor != f"user:{command.actor_system_user_id}"
            or not context.reason.strip()
            or len(context.reason) > 1000
            or not context.idempotency_key
        ):
            raise _error(
                "approval_permission_required",
                "Named staff approval, reason and idempotency evidence are required.",
            )
        require_time_credit_staff(
            db, command.actor_system_user_id, OUTAGE_APPROVAL_SCOPE
        )
        original = db.get(OutageCompensationDecision, command.decision_id)
        if original is None:
            raise _error("review_invalid", "Compensation proposal was not found.")
        lock_account(db, str(original.account_id))
        db.refresh(original, with_for_update=True)
        maker = original.created_by.rsplit(":", 1)[-1]
        if maker == str(command.actor_system_user_id):
            raise _error(
                "self_approval_forbidden",
                "A compensation proposal must be approved by a different staff member.",
            )
        if original.resolved_by_decision_id is not None:
            approved = db.get(
                OutageCompensationDecision, original.resolved_by_decision_id
            )
            if (
                approved is None
                or approved.approved_by is None
                or approved.approved_fingerprint != command.expected_fingerprint
                or approved.idempotency_key != context.idempotency_key
            ):
                raise _error(
                    "idempotency_conflict",
                    "This proposal already has a different approval.",
                )
            return OutageCompensationResult(
                approved.id,
                approved.status,
                approved.entitlement_id,
                approved.funded_overlap_seconds,
                approved.tail_after,
                True,
            )
        preview = preview_outage_compensation(
            db,
            subscription_id=original.subscription_id,
            effective_at=datetime.now(UTC),
            review_decision_id=original.id,
        )
        if preview.fingerprint != command.expected_fingerprint:
            raise _error(
                "stale_preview",
                "Funding, policy or downtime changed; review a fresh proposal.",
            )
        if preview.status is not OutageCompensationDecisionStatus.compensated:
            raise _error(
                "review_required",
                "This proposal requires evidence review and cannot be posted.",
            )
        result = _stage_outage_compensation(
            db,
            ApplyOutageCompensationCommand(
                subscription_id=original.subscription_id,
                expected_fingerprint=command.expected_fingerprint,
                idempotency_key=context.idempotency_key,
                effective_at=command.effective_at,
                context=context,
                review_decision_id=original.id,
            ),
            approved_by=command.actor_system_user_id,
        )
        AuditEvents.stage(
            db,
            AuditEventCreate(
                actor_type=AuditActorType.user,
                actor_id=str(command.actor_system_user_id),
                actor_label=context.actor,
                action="outage_compensation.approved",
                entity_type="outage_compensation_decision",
                entity_id=str(result.decision_id),
                metadata_={
                    "proposal_id": str(original.id),
                    "preview_fingerprint": command.expected_fingerprint,
                    "reason": context.reason,
                },
            ),
        )
        db.flush()
        return result

    return execute_owner_command(
        db, definition=_APPROVE_COMMAND, context=context, operation=operation
    )


@dataclass(frozen=True, slots=True)
class LegacyTimeCreditPreview:
    entry_id: UUID
    subscription_id: UUID
    ranges: tuple[TimeInterval, ...]
    fingerprint: str
    source_seconds: int
    grant_seconds: int


@dataclass(frozen=True, slots=True)
class AttestLegacyTimeCreditCommand:
    entry_id: UUID
    ranges: tuple[TimeInterval, ...]
    expected_fingerprint: str
    actor_system_user_id: UUID


def preview_legacy_time_credit(
    db: Session, entry_id: UUID, *, ranges: tuple[TimeInterval, ...] | None = None
) -> LegacyTimeCreditPreview:
    from app.models.service_extension import (
        ServiceExtension,
        ServiceExtensionEntry,
        ServiceExtensionStatus,
    )

    entry = db.get(ServiceExtensionEntry, entry_id)
    extension = db.get(ServiceExtension, entry.extension_id) if entry else None
    if (
        entry is None
        or extension is None
        or extension.status is not ServiceExtensionStatus.applied
        or entry.grant_starts_at is None
        or entry.grant_ends_at is None
    ):
        raise _error(
            "legacy_credit_invalid",
            "An applied extension with exact grant evidence is required.",
        )
    window = (TimeInterval(_utc(extension.window_start), _utc(extension.window_end)),)
    reviewed = merge_intervals(list(ranges if ranges is not None else window))
    grant_seconds = int(
        (_utc(entry.grant_ends_at) - _utc(entry.grant_starts_at)).total_seconds()
    )
    if (
        not reviewed
        or reviewed != (ranges if ranges is not None else window)
        or intersect_intervals(reviewed, window) != reviewed
        or interval_seconds(reviewed) > grant_seconds
    ):
        raise _error(
            "legacy_credit_invalid",
            "Reviewed clock ranges must be inside the original outage window and backed by the granted time.",
        )
    payload = {
        "entry": str(entry.id),
        "subscription": str(entry.subscription_id),
        "extension": str(extension.id),
        "window": [window[0].starts_at.isoformat(), window[0].ends_at.isoformat()],
        "grant": [str(entry.grant_starts_at), str(entry.grant_ends_at)],
        "ranges": [
            [item.starts_at.isoformat(), item.ends_at.isoformat()] for item in reviewed
        ],
    }
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()
    return LegacyTimeCreditPreview(
        entry.id,
        entry.subscription_id,
        reviewed,
        fingerprint,
        interval_seconds(window),
        grant_seconds,
    )


def attest_legacy_time_credit(
    db: Session, command: AttestLegacyTimeCreditCommand, *, context: CommandContext
) -> LegacyTimeCreditPreview:
    definition = OwnerCommandDefinition(
        owner=_OWNER,
        concern="reviewed legacy time credit attestation",
        name="attest_legacy_time_credit",
    )

    def operation() -> LegacyTimeCreditPreview:
        if (
            context.scope != OUTAGE_REPAIR_SCOPE
            or context.actor
            not in {
                f"user:{command.actor_system_user_id}",
                f"staff:{command.actor_system_user_id}",
            }
            or not context.reason.strip()
            or len(context.reason) > 1000
            or not context.idempotency_key
        ):
            raise _error(
                "repair_permission_required", "Named staff repair evidence is required."
            )
        require_time_credit_staff(db, command.actor_system_user_id, OUTAGE_REPAIR_SCOPE)
        preview = preview_legacy_time_credit(
            db, command.entry_id, ranges=command.ranges
        )
        subscription = db.get(Subscription, preview.subscription_id)
        if subscription is None:
            raise _error("subscription_not_found", "Subscription was not found.")
        lock_account(db, str(subscription.subscriber_id))
        preview = preview_legacy_time_credit(
            db, command.entry_id, ranges=command.ranges
        )
        if preview.fingerprint != command.expected_fingerprint:
            raise _error("stale_preview", "Legacy compensation evidence changed.")
        from app.models.service_period_purchase import CompensatedServiceTime
        from app.services.compensated_service_time import (
            StageTimeCreditCommand,
            TimeCreditSource,
            stage_compensated_service_time,
        )

        replay = (
            db.scalar(
                select(CompensatedServiceTime.id)
                .where(
                    CompensatedServiceTime.source_kind == "extension",
                    CompensatedServiceTime.source_id == command.entry_id,
                )
                .limit(1)
            )
            is not None
        )
        stage_compensated_service_time(
            db,
            StageTimeCreditCommand(
                subscription_id=preview.subscription_id,
                source=TimeCreditSource.extension,
                source_id=preview.entry_id,
                ranges=preview.ranges,
                evidence_ref=f"staff-attestation:{command.actor_system_user_id}:{preview.fingerprint}",
            ),
        )
        if not replay:
            AuditEvents.stage(
                db,
                AuditEventCreate(
                    actor_type=AuditActorType.user,
                    actor_id=str(command.actor_system_user_id),
                    actor_label=context.actor,
                    action="time_credit.legacy_attested",
                    entity_type="service_extension_entry",
                    entity_id=str(command.entry_id),
                    metadata_={
                        "reason": context.reason,
                        "preview_fingerprint": preview.fingerprint,
                    },
                ),
            )
            from app.services.events import emit_event
            from app.services.events.types import EventType

            emit_event(
                db,
                EventType.time_credit_attested,
                {
                    "schema_version": 1,
                    "source_id": str(command.entry_id),
                    "preview_fingerprint": preview.fingerprint,
                },
                actor=context.actor,
                subscription_id=preview.subscription_id,
            )
        return preview

    return execute_owner_command(
        db, definition=definition, context=context, operation=operation
    )


@dataclass(frozen=True, slots=True)
class RevokeOutageCompensationFundingCommand:
    source_entitlement_ids: tuple[UUID, ...]
    evidence_ref: str


def stage_revoke_outage_compensation_funding(
    db: Session, command: RevokeOutageCompensationFundingCommand
) -> tuple[UUID, ...]:
    """Retract dependent grants when the entitlement owner retracts funding."""
    lost = {str(item) for item in command.source_entitlement_ids}
    revoked: list[UUID] = []
    if not lost:
        return ()
    if not command.evidence_ref.strip():
        raise _error("review_invalid", "Funding-retraction evidence is required.")
    reversed_sources = tuple(
        db.scalars(
            select(ServiceEntitlement)
            .where(
                ServiceEntitlement.id.in_(command.source_entitlement_ids),
            )
            .with_for_update()
        ).all()
    )
    if len(reversed_sources) != len(set(command.source_entitlement_ids)) or any(
        row.status is not ServiceEntitlementStatus.reversed for row in reversed_sources
    ):
        raise _error(
            "review_invalid",
            "Only confirmed reversed funding can retract compensation.",
        )
    subscription_ids = tuple(row.subscription_id for row in reversed_sources)
    grants = list(
        db.scalars(
            select(ServiceEntitlement).where(
                ServiceEntitlement.subscription_id.in_(subscription_ids),
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
                ServiceEntitlement.source_outage_compensation_id.is_not(None),
            )
        ).all()
    )
    while True:
        changed = False
        for grant in grants:
            if str(grant.id) in lost:
                continue
            decision = db.get(
                OutageCompensationDecision, grant.source_outage_compensation_id
            )
            sources = (
                set((decision.policy_snapshot or {}).get("funded_entitlement_ids", []))
                if decision
                else set()
            )
            if not sources or sources & lost:
                grant.status = ServiceEntitlementStatus.reversed
                grant.metadata_ = {
                    **(grant.metadata_ or {}),
                    "revoked_reason": "compensation_source_funding_retracted",
                    "revoked_evidence_ref": command.evidence_ref,
                }
                revoked.append(grant.id)
                lost.add(str(grant.id))
                changed = True
        if not changed:
            break
    db.flush()
    return tuple(revoked)


def consume_outage_compensation_event(
    db: Session,
    *,
    subscription_id: UUID,
    event_id: UUID,
    event_type: str,
    effective_at: datetime,
    context: CommandContext,
) -> str:
    """Consume one resolved/discarded outage output for one subscription."""
    from app.services.events.owner_outputs import consume_owner_output

    def _consume() -> str:
        def _effect() -> str:
            outcomes: list[str] = []
            sequence = 0
            while True:
                try:
                    preview = preview_outage_compensation(
                        db,
                        subscription_id=subscription_id,
                        effective_at=effective_at,
                    )
                except OutageCompensationError as exc:
                    if exc.code == f"{_OWNER}.no_finalized_outage":
                        break
                    raise
                result = _stage_outage_compensation(
                    db,
                    ApplyOutageCompensationCommand(
                        subscription_id=subscription_id,
                        expected_fingerprint=preview.fingerprint,
                        idempotency_key=(
                            f"outage-event:{event_id}:{subscription_id}:{sequence}"
                        ),
                        effective_at=effective_at,
                        context=context,
                    ),
                )
                outcomes.append(result.status.value)
                sequence += 1
            return ",".join(outcomes) or "noop"

        outcome, _receipt = consume_owner_output(
            db,
            consumer=f"financial.outage_compensation:{subscription_id}",
            event_id=event_id,
            event_type=event_type,
            producer_owner="network.outage_lifecycle",
            context=context,
            operation=_effect,
        )
        return outcome or "replayed"

    return execute_owner_command(
        db,
        definition=_CONSUME_COMMAND,
        context=context,
        operation=_consume,
    )


__all__ = [
    "ApproveOutageCompensationCommand",
    "approve_outage_compensation",
    "OUTAGE_APPROVAL_SCOPE",
    "ReviewOutageCompensationCommand",
    "review_outage_compensation",
    "ApplyOutageCompensationCommand",
    "OutageCompensationError",
    "OutageCompensationPreview",
    "OutageCompensationResult",
    "TimeInterval",
    "apply_outage_compensation",
    "consume_outage_compensation_event",
    "intersect_seconds",
    "merge_intervals",
    "preview_outage_compensation",
    "RevokeOutageCompensationFundingCommand",
    "stage_revoke_outage_compensation_funding",
]
