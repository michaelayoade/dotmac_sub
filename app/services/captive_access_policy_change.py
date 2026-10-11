"""Preview and apply a captive access policy change, re-evaluating locks.

Owner: ``access.captive_access_policy_change`` (application coordinator).

An enforcement lock stores the effective treatment granted when it was
created, so a later policy change (an opt-in after suspension, a newly ready
router, a revoked rule) does not reach existing locks by itself. This
coordinator closes that gap:

* :func:`preview_captive_policy_change` (query, writes nothing) evaluates a
  candidate change against every subscription holding an active lock that
  REQUESTED captive, and returns the lock updates and the subscriptions that
  move hard_reject -> captive or captive -> hard_reject, grouped by serving
  router and plan family, plus an exact fingerprint.
* :func:`apply_captive_policy_change` (owner command) re-derives the same plan
  inside one transaction, refuses a stale fingerprint, writes the rule/cohort
  change through ``access.captive_access_policy``, verifies the written policy
  reproduces the preview, updates lock access modes through the lifecycle
  owner (``access.subscription_lifecycle``) for a bounded batch of
  subscriptions, and stages audit and domain events. Each changed lock emits
  ``enforcement_lock.access_mode_changed``; after commit the durable
  dispatcher hands it to the enforcement handler, which reprojects RADIUS
  through ``radius.reconcile_subscription_connectivity`` and enqueues the
  existing session-cleanup (CoA/disconnect) task. Re-running with a
  ``ReevaluateCaptivePolicy`` change drains the remaining batches.

Idempotency: the command's ``idempotency_key`` is unique; a replay with the
same change and preview fingerprint returns the stored outcome, any other
reuse fails closed.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models.audit import AuditActorType
from app.models.captive_access_policy import CaptiveAccessPolicyChange
from app.models.catalog import Subscription
from app.models.enforcement_lock import AccessRestrictionMode, EnforcementLock
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.services.account_lifecycle import (
    ReevaluateLockAccessModeCommand,
    reevaluate_enforcement_lock_access_modes,
)
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.auth_dependencies import has_permission
from app.services.captive_access_policy import (
    CaptivePolicyChange,
    CaptivePolicySnapshot,
    change_fingerprint,
    change_kind,
    change_payload,
    load_captive_policy_snapshot,
    project_change,
    stage_captive_policy_change,
)
from app.services.captive_router_gate import CaptiveRouterGate
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.system_user_assignments import system_user_role_names
from app.services.walled_garden_policy import (
    WalledGardenEvaluation,
    resolve_restriction_from_lock_modes,
    resolve_walled_garden_decision,
)

OWNER = "access.captive_access_policy_change"
APPLY_PERMISSION = "network:radius:write"
DEFAULT_MAX_SUBSCRIPTIONS = 200
MAX_SUBSCRIPTIONS_LIMIT = 1000
_ADVISORY_LOCK_KEY = 4127730518

_APPLY_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="captive access policy change and lock re-evaluation",
    name="apply_captive_policy_change",
)


class CaptivePolicyChangeErrorCode:
    INVALID_COMMAND = f"{OWNER}.invalid_command"
    PERMISSION_DENIED = f"{OWNER}.permission_denied"
    STALE_PREVIEW = f"{OWNER}.stale_preview"
    IDEMPOTENCY_CONFLICT = f"{OWNER}.idempotency_conflict"
    APPLY_DIVERGED = f"{OWNER}.apply_diverged_from_preview"

    ALL: tuple[str, ...] = (
        INVALID_COMMAND,
        PERMISSION_DENIED,
        STALE_PREVIEW,
        IDEMPOTENCY_CONFLICT,
        APPLY_DIVERGED,
    )


class CaptivePolicyChangeError(DomainError):
    """Stable, transport-neutral refusal of a preview/apply request."""


def _error(code: str, message: str, **details: object) -> CaptivePolicyChangeError:
    return CaptivePolicyChangeError(
        code=code, message=message, details=details, retryable=False
    )


class CaptiveMoveDirection(StrEnum):
    to_captive = "to_captive"
    to_hard_reject = "to_hard_reject"


@dataclass(frozen=True, slots=True)
class PlannedLockUpdate:
    lock_id: UUID
    subscription_id: UUID
    from_mode: AccessRestrictionMode
    to_mode: AccessRestrictionMode
    reason: str


@dataclass(frozen=True, slots=True)
class CaptiveAccessMove:
    subscription_id: UUID
    subscriber_id: UUID
    plan_family: str | None
    router_ids: tuple[UUID, ...]
    router_names: tuple[str, ...]
    direction: CaptiveMoveDirection
    reason: str


@dataclass(frozen=True, slots=True)
class CaptiveMoveCount:
    direction: CaptiveMoveDirection
    key: str
    count: int


@dataclass(frozen=True, slots=True)
class PreviewCaptivePolicyChangeQuery:
    change: CaptivePolicyChange
    actor: str
    evaluated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CaptivePolicyChangePreview:
    change_kind: str
    change_fingerprint: str
    preview_fingerprint: str
    evaluated_at: datetime
    subscriptions_evaluated: int
    lock_updates: tuple[PlannedLockUpdate, ...]
    moves: tuple[CaptiveAccessMove, ...]
    by_router: tuple[CaptiveMoveCount, ...]
    by_plan_family: tuple[CaptiveMoveCount, ...]

    @property
    def to_captive(self) -> tuple[CaptiveAccessMove, ...]:
        return tuple(
            item
            for item in self.moves
            if item.direction is CaptiveMoveDirection.to_captive
        )

    @property
    def to_hard_reject(self) -> tuple[CaptiveAccessMove, ...]:
        return tuple(
            item
            for item in self.moves
            if item.direction is CaptiveMoveDirection.to_hard_reject
        )

    @property
    def subscriptions_with_lock_updates(self) -> tuple[UUID, ...]:
        return tuple(sorted({item.subscription_id for item in self.lock_updates}))


@dataclass(frozen=True, slots=True)
class ApplyCaptivePolicyChangeCommand:
    context: CommandContext
    change: CaptivePolicyChange
    expected_preview_fingerprint: str
    authorized_system_user_id: UUID
    evaluated_at: datetime | None = None
    max_subscriptions: int = DEFAULT_MAX_SUBSCRIPTIONS


@dataclass(frozen=True, slots=True)
class CaptivePolicyChangeOutcome:
    change_id: UUID
    change_kind: str
    preview_fingerprint: str
    rule_id: UUID | None
    customer_set_id: UUID | None
    members_added: int
    members_removed: int
    members_unchanged: int
    lock_updates_applied: int
    subscriptions_applied: tuple[UUID, ...]
    moves_applied: tuple[CaptiveAccessMove, ...]
    remaining_subscriptions: int
    replayed: bool = False


# ---------------------------------------------------------------------------
# Planning (shared by preview and apply)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Plan:
    subscriptions_evaluated: int
    lock_updates: tuple[PlannedLockUpdate, ...]
    moves: tuple[CaptiveAccessMove, ...]


def _candidate_subscriptions(db: Session) -> list[Subscription]:
    subscription_ids = (
        select(EnforcementLock.subscription_id)
        .where(EnforcementLock.is_active.is_(True))
        .where(EnforcementLock.requested_access_mode == AccessRestrictionMode.captive)
        .distinct()
    )
    return list(
        db.scalars(
            select(Subscription)
            .options(
                joinedload(Subscription.subscriber).joinedload(Subscriber.reseller),
                joinedload(Subscription.offer),
            )
            .where(Subscription.id.in_(subscription_ids))
            .order_by(Subscription.id)
        )
        .unique()
        .all()
    )


def _plan(
    db: Session,
    *,
    current: CaptivePolicySnapshot,
    candidate: CaptivePolicySnapshot,
    evaluated_at: datetime,
) -> _Plan:
    subscriptions = _candidate_subscriptions(db)
    locks_by_subscription: dict[UUID, list[EnforcementLock]] = {}
    if subscriptions:
        for lock in db.scalars(
            select(EnforcementLock)
            .where(
                EnforcementLock.subscription_id.in_([item.id for item in subscriptions])
            )
            .where(EnforcementLock.is_active.is_(True))
            .order_by(EnforcementLock.id)
        ).all():
            locks_by_subscription.setdefault(lock.subscription_id, []).append(lock)
    gate = CaptiveRouterGate(db, evaluated_at=evaluated_at)
    gate.prefetch(subscriptions)
    before_eval = WalledGardenEvaluation(db, policy=current, router_gate=gate)
    after_eval = WalledGardenEvaluation(db, policy=candidate, router_gate=gate)

    lock_updates: list[PlannedLockUpdate] = []
    moves: list[CaptiveAccessMove] = []
    for subscription in subscriptions:
        account = subscription.subscriber
        if account is None:
            continue
        locks = locks_by_subscription.get(subscription.id, [])
        target_modes: list[AccessRestrictionMode] = []
        for lock in locks:
            if lock.requested_access_mode == AccessRestrictionMode.captive:
                decision = resolve_walled_garden_decision(
                    db,
                    account,
                    requested_mode=AccessRestrictionMode.captive,
                    subscription=subscription,
                    evaluation=after_eval,
                )
                target, reason = decision.effective_mode, decision.reason.value
            else:
                target, reason = (
                    AccessRestrictionMode.hard_reject,
                    "hard_reject_requested",
                )
            target_modes.append(target)
            if target != lock.access_mode:
                lock_updates.append(
                    PlannedLockUpdate(
                        lock_id=lock.id,
                        subscription_id=subscription.id,
                        from_mode=lock.access_mode,
                        to_mode=target,
                        reason=reason,
                    )
                )
        before = resolve_restriction_from_lock_modes(
            db,
            subscription,
            account=account,
            lock_modes=[lock.access_mode for lock in locks],
            evaluation=before_eval,
        )
        after = resolve_restriction_from_lock_modes(
            db,
            subscription,
            account=account,
            lock_modes=target_modes,
            evaluation=after_eval,
        )
        if before is None or after is None:
            continue
        if before.effective_mode == after.effective_mode:
            continue
        gate_decision = gate.decide(subscription)
        offer = subscription.offer
        moves.append(
            CaptiveAccessMove(
                subscription_id=subscription.id,
                subscriber_id=account.id,
                plan_family=(
                    str(offer.plan_family).strip().lower() or None
                    if offer is not None and offer.plan_family
                    else None
                ),
                router_ids=gate_decision.router_ids,
                router_names=gate_decision.router_names,
                direction=(
                    CaptiveMoveDirection.to_captive
                    if after.effective_mode == AccessRestrictionMode.captive
                    else CaptiveMoveDirection.to_hard_reject
                ),
                reason=after.reason.value,
            )
        )
    return _Plan(
        subscriptions_evaluated=len(subscriptions),
        lock_updates=tuple(lock_updates),
        moves=tuple(moves),
    )


def _preview_fingerprint(change: CaptivePolicyChange, plan: _Plan) -> str:
    payload = {
        "change": change_payload(change),
        "lock_updates": [
            [str(item.lock_id), item.from_mode.value, item.to_mode.value]
            for item in plan.lock_updates
        ],
        "moves": [
            [str(item.subscription_id), item.direction.value] for item in plan.moves
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _counts(
    moves: Sequence[CaptiveAccessMove], *, by_router: bool
) -> tuple[CaptiveMoveCount, ...]:
    counter: Counter[tuple[CaptiveMoveDirection, str]] = Counter()
    for move in moves:
        keys = (
            (move.router_names or ("(unresolved)",))
            if by_router
            else (move.plan_family or "(none)",)
        )
        for key in keys:
            counter[(move.direction, key)] += 1
    return tuple(
        CaptiveMoveCount(direction=direction, key=key, count=count)
        for (direction, key), count in sorted(
            counter.items(), key=lambda item: (item[0][0].value, item[0][1])
        )
    )


def _aware(value: datetime | None) -> datetime:
    resolved = value or datetime.now(UTC)
    return resolved if resolved.tzinfo is not None else resolved.replace(tzinfo=UTC)


def _build_preview(
    db: Session, *, change: CaptivePolicyChange, actor: str, evaluated_at: datetime
) -> tuple[CaptivePolicyChangePreview, CaptivePolicySnapshot]:
    current = load_captive_policy_snapshot(db)
    candidate = project_change(db, current, change, actor=actor, now=evaluated_at)
    plan = _plan(db, current=current, candidate=candidate, evaluated_at=evaluated_at)
    preview = CaptivePolicyChangePreview(
        change_kind=change_kind(change),
        change_fingerprint=change_fingerprint(change),
        preview_fingerprint=_preview_fingerprint(change, plan),
        evaluated_at=evaluated_at,
        subscriptions_evaluated=plan.subscriptions_evaluated,
        lock_updates=plan.lock_updates,
        moves=plan.moves,
        by_router=_counts(plan.moves, by_router=True),
        by_plan_family=_counts(plan.moves, by_router=False),
    )
    return preview, candidate


def preview_captive_policy_change(
    db: Session, *, query: PreviewCaptivePolicyChangeQuery
) -> CaptivePolicyChangePreview:
    """Evaluate a candidate change without writing anything."""

    preview, _ = _build_preview(
        db,
        change=query.change,
        actor=query.actor,
        evaluated_at=_aware(query.evaluated_at),
    )
    return preview


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _move_json(move: CaptiveAccessMove) -> dict[str, object]:
    return {
        "subscription_id": str(move.subscription_id),
        "subscriber_id": str(move.subscriber_id),
        "plan_family": move.plan_family,
        "router_ids": [str(item) for item in move.router_ids],
        "router_names": list(move.router_names),
        "direction": move.direction.value,
        "reason": move.reason,
    }


def _list(value: object) -> list[object]:
    """Stored outcome JSON is written by this owner; anything else is corrupt."""

    if value is None:
        return []
    if not isinstance(value, list):
        raise _error(
            CaptivePolicyChangeErrorCode.IDEMPOTENCY_CONFLICT,
            "Stored change outcome is malformed and cannot be replayed.",
        )
    return value


def _move_from_json(data: dict[str, object]) -> CaptiveAccessMove:
    router_ids = _list(data.get("router_ids"))
    router_names = _list(data.get("router_names"))
    plan_family = data.get("plan_family")
    return CaptiveAccessMove(
        subscription_id=UUID(str(data["subscription_id"])),
        subscriber_id=UUID(str(data["subscriber_id"])),
        plan_family=str(plan_family) if plan_family is not None else None,
        router_ids=tuple(UUID(str(item)) for item in router_ids),
        router_names=tuple(str(item) for item in router_names),
        direction=CaptiveMoveDirection(str(data["direction"])),
        reason=str(data["reason"]),
    )


def _outcome_json(outcome: CaptivePolicyChangeOutcome) -> dict[str, object]:
    return {
        "change_id": str(outcome.change_id),
        "change_kind": outcome.change_kind,
        "preview_fingerprint": outcome.preview_fingerprint,
        "rule_id": str(outcome.rule_id) if outcome.rule_id else None,
        "customer_set_id": (
            str(outcome.customer_set_id) if outcome.customer_set_id else None
        ),
        "members_added": outcome.members_added,
        "members_removed": outcome.members_removed,
        "members_unchanged": outcome.members_unchanged,
        "lock_updates_applied": outcome.lock_updates_applied,
        "subscriptions_applied": [str(item) for item in outcome.subscriptions_applied],
        "moves_applied": [_move_json(item) for item in outcome.moves_applied],
        "remaining_subscriptions": outcome.remaining_subscriptions,
    }


def _int(value: object) -> int:
    return int(str(value))


def _optional_uuid(value: object) -> UUID | None:
    return UUID(str(value)) if value else None


def _outcome_from_json(data: dict[str, object]) -> CaptivePolicyChangeOutcome:
    subscriptions = _list(data.get("subscriptions_applied"))
    moves = [
        item for item in _list(data.get("moves_applied")) if isinstance(item, dict)
    ]
    return CaptivePolicyChangeOutcome(
        change_id=UUID(str(data["change_id"])),
        change_kind=str(data["change_kind"]),
        preview_fingerprint=str(data["preview_fingerprint"]),
        rule_id=_optional_uuid(data.get("rule_id")),
        customer_set_id=_optional_uuid(data.get("customer_set_id")),
        members_added=_int(data.get("members_added", 0)),
        members_removed=_int(data.get("members_removed", 0)),
        members_unchanged=_int(data.get("members_unchanged", 0)),
        lock_updates_applied=_int(data.get("lock_updates_applied", 0)),
        subscriptions_applied=tuple(UUID(str(item)) for item in subscriptions),
        moves_applied=tuple(_move_from_json(dict(item)) for item in moves),
        remaining_subscriptions=_int(data.get("remaining_subscriptions", 0)),
        replayed=True,
    )


def principal_label(system_user_id: UUID) -> str:
    return f"system_user:{system_user_id}"


def _verify_principal(db: Session, command: ApplyCaptivePolicyChangeCommand) -> None:
    if command.context.actor != principal_label(command.authorized_system_user_id):
        raise _error(
            CaptivePolicyChangeErrorCode.PERMISSION_DENIED,
            "The command actor must be the authorized staff principal.",
        )
    user = db.get(SystemUser, command.authorized_system_user_id, populate_existing=True)
    if user is None or not user.is_active:
        raise _error(
            CaptivePolicyChangeErrorCode.PERMISSION_DENIED,
            "Captive policy changes require an active staff principal.",
        )
    granted = has_permission(
        {
            "principal_id": str(command.authorized_system_user_id),
            "principal_type": "system_user",
            "roles": set(system_user_role_names(db, command.authorized_system_user_id)),
        },
        db,
        APPLY_PERMISSION,
    )
    if not granted:
        raise _error(
            CaptivePolicyChangeErrorCode.PERMISSION_DENIED,
            f"Captive policy changes require the {APPLY_PERMISSION} permission.",
        )


def _validate_command(command: ApplyCaptivePolicyChangeCommand) -> str:
    key = (command.context.idempotency_key or "").strip()
    if not key or len(key) > 200:
        raise _error(
            CaptivePolicyChangeErrorCode.INVALID_COMMAND,
            "An idempotency key of 1-200 characters is required.",
        )
    if not 1 <= command.max_subscriptions <= MAX_SUBSCRIPTIONS_LIMIT:
        raise _error(
            CaptivePolicyChangeErrorCode.INVALID_COMMAND,
            f"max_subscriptions must be between 1 and {MAX_SUBSCRIPTIONS_LIMIT}.",
        )
    if len(command.expected_preview_fingerprint.strip()) != 64:
        raise _error(
            CaptivePolicyChangeErrorCode.INVALID_COMMAND,
            "The exact preview fingerprint is required.",
        )
    return key


def _serialize(db: Session) -> None:
    """Serialize concurrent policy changes (PostgreSQL transaction lock)."""

    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        db.execute(select(func.pg_advisory_xact_lock(_ADVISORY_LOCK_KEY)))


def _apply(
    db: Session, command: ApplyCaptivePolicyChangeCommand
) -> CaptivePolicyChangeOutcome:
    key = _validate_command(command)
    context = command.context
    fingerprint = change_fingerprint(command.change)
    _serialize(db)
    _verify_principal(db, command)
    existing = db.scalar(
        select(CaptiveAccessPolicyChange).where(
            CaptiveAccessPolicyChange.idempotency_key == key
        )
    )
    if existing is not None:
        if (
            existing.change_fingerprint != fingerprint
            or existing.preview_fingerprint != command.expected_preview_fingerprint
        ):
            raise _error(
                CaptivePolicyChangeErrorCode.IDEMPOTENCY_CONFLICT,
                "This idempotency key was used for a different change.",
            )
        return _outcome_from_json(dict(existing.outcome))

    evaluated_at = _aware(command.evaluated_at)
    preview, _ = _build_preview(
        db, change=command.change, actor=context.actor, evaluated_at=evaluated_at
    )
    if preview.preview_fingerprint != command.expected_preview_fingerprint:
        raise _error(
            CaptivePolicyChangeErrorCode.STALE_PREVIEW,
            "Policy, lock, or router state changed after the preview; preview again.",
        )

    staged = stage_captive_policy_change(
        db, command.change, context=context, now=evaluated_at
    )
    # Verify: the policy as written must reproduce exactly the previewed plan.
    written = load_captive_policy_snapshot(db)
    verified = _plan(db, current=written, candidate=written, evaluated_at=evaluated_at)
    if verified.lock_updates != preview.lock_updates:
        raise _error(
            CaptivePolicyChangeErrorCode.APPLY_DIVERGED,
            "The written policy does not reproduce the previewed lock updates.",
        )

    batch = preview.subscriptions_with_lock_updates[: command.max_subscriptions]
    batch_ids = frozenset(batch)
    change_row = CaptiveAccessPolicyChange(
        idempotency_key=key,
        command_id=context.command_id,
        change_kind=staged.kind,
        change_fingerprint=fingerprint,
        preview_fingerprint=preview.preview_fingerprint,
        actor=context.actor,
        reason=context.reason,
        max_subscriptions=command.max_subscriptions,
        outcome={},
        created_at=evaluated_at,
    )
    db.add(change_row)
    db.flush()
    applied = reevaluate_enforcement_lock_access_modes(
        db,
        tuple(
            ReevaluateLockAccessModeCommand(
                lock_id=item.lock_id,
                expected_access_mode=item.from_mode,
                target_access_mode=item.to_mode,
                decision_reason=item.reason,
            )
            for item in preview.lock_updates
            if item.subscription_id in batch_ids
        ),
        source=f"captive_access_policy_change:{change_row.id}",
    )
    outcome = CaptivePolicyChangeOutcome(
        change_id=change_row.id,
        change_kind=staged.kind,
        preview_fingerprint=preview.preview_fingerprint,
        rule_id=staged.rule_id,
        customer_set_id=staged.customer_set_id,
        members_added=staged.members_added,
        members_removed=staged.members_removed,
        members_unchanged=staged.members_unchanged,
        lock_updates_applied=len(applied),
        subscriptions_applied=batch,
        moves_applied=tuple(
            item for item in preview.moves if item.subscription_id in batch_ids
        ),
        remaining_subscriptions=len(preview.subscriptions_with_lock_updates)
        - len(batch),
    )
    change_row.outcome = _outcome_json(outcome)
    stage_audit_event(
        db,
        action="access.captive_access_policy_change_applied",
        entity_type="captive_access_policy_change",
        entity_id=str(change_row.id),
        actor=AuditActor(
            actor_type=AuditActorType.user,
            actor_id=str(command.authorized_system_user_id),
            label=context.actor,
        ),
        request_id=str(context.correlation_id),
        metadata={
            "command_id": str(context.command_id),
            "change": change_payload(command.change),
            "reason": context.reason,
            "preview_fingerprint": preview.preview_fingerprint,
            "lock_updates_applied": outcome.lock_updates_applied,
            "to_captive": sum(
                1
                for item in outcome.moves_applied
                if item.direction is CaptiveMoveDirection.to_captive
            ),
            "to_hard_reject": sum(
                1
                for item in outcome.moves_applied
                if item.direction is CaptiveMoveDirection.to_hard_reject
            ),
            "remaining_subscriptions": outcome.remaining_subscriptions,
        },
    )
    db.flush()
    return outcome


def apply_captive_policy_change(
    db: Session, command: ApplyCaptivePolicyChangeCommand
) -> CaptivePolicyChangeOutcome:
    """Apply one previewed change and re-evaluate a bounded batch of locks."""

    return execute_owner_command(
        db,
        definition=_APPLY_COMMAND,
        context=command.context,
        operation=lambda: _apply(db, command),
    )


__all__ = [
    "APPLY_PERMISSION",
    "DEFAULT_MAX_SUBSCRIPTIONS",
    "ApplyCaptivePolicyChangeCommand",
    "CaptiveAccessMove",
    "CaptiveMoveCount",
    "CaptiveMoveDirection",
    "CaptivePolicyChangeError",
    "CaptivePolicyChangeErrorCode",
    "CaptivePolicyChangeOutcome",
    "CaptivePolicyChangePreview",
    "PlannedLockUpdate",
    "PreviewCaptivePolicyChangeQuery",
    "apply_captive_policy_change",
    "preview_captive_policy_change",
    "principal_label",
]
