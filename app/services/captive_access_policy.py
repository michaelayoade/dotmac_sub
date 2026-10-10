"""Composable captive access policy: typed rules, cohorts, and resolution.

Owner: ``access.captive_access_policy``.

* Resolver (read-only): :func:`load_captive_policy_snapshot` reads enabled
  rules and open customer-set memberships once per run;
  :meth:`CaptivePolicySnapshot.resolve` decides, per SUBSCRIPTION, whether a
  rule allows the captive tier.
* Participant writer: :func:`stage_captive_policy_change` writes rule and
  cohort records flush-only, and only inside the
  ``access.captive_access_policy_change`` coordinator command, which also
  re-evaluates existing enforcement locks in the same transaction.

Resolution: the most specific matching scope wins (``account`` >
``customer_set`` > ``plan_family`` > ``global``); at the same scope ``deny``
beats ``allow``; with no matching rule the result is a default deny (hard
reject). A rule matches only when its target AND its optional conditions
(subscriber category, house or specific reseller) hold. Category and reseller
are conditions, not hard-coded eligibility, so business accounts can be
enabled deliberately. Fixed safety rails (non-customer principals, inactive
accounts, disabled/terminal services) live in ``access.walled_garden_policy``
and cannot be overridden by any rule.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.captive_access_policy import (
    CaptiveAccessRule,
    CaptiveAccessRuleEffect,
    CaptiveAccessRuleScope,
    CaptiveCustomerSet,
    CaptiveCustomerSetMember,
    CaptiveResellerCondition,
)
from app.models.catalog import Subscription
from app.models.subscriber import Reseller, Subscriber, SubscriberCategory
from app.services.audit_adapter import AuditActor, stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import CommandContext, owner_command_active

OWNER = "access.captive_access_policy"
COORDINATOR = "access.captive_access_policy_change"

#: Resolution order, most specific first.
SCOPE_PRECEDENCE: tuple[CaptiveAccessRuleScope, ...] = (
    CaptiveAccessRuleScope.account,
    CaptiveAccessRuleScope.customer_set,
    CaptiveAccessRuleScope.plan_family,
    CaptiveAccessRuleScope.global_,
)

_MIN_REASON = 10
_MAX_REASON = 2000
_MAX_MEMBERS_PER_CHANGE = 5000

#: Deterministic placeholder id for a not-yet-written rule in a preview. It is
#: never persisted and never part of a preview fingerprint.
CANDIDATE_RULE_ID = UUID(int=0)
CANDIDATE_SET_ID = UUID(int=1)


class CaptiveAccessPolicyErrorCode:
    INVALID_CHANGE = f"{OWNER}.invalid_change"
    RULE_NOT_FOUND = f"{OWNER}.rule_not_found"
    CUSTOMER_SET_NOT_FOUND = f"{OWNER}.customer_set_not_found"
    SUBSCRIBER_NOT_FOUND = f"{OWNER}.subscriber_not_found"
    DUPLICATE_RULE = f"{OWNER}.duplicate_rule"
    DUPLICATE_CUSTOMER_SET = f"{OWNER}.duplicate_customer_set"
    OUTSIDE_COORDINATOR = f"{OWNER}.outside_coordinator"

    ALL: tuple[str, ...] = (
        INVALID_CHANGE,
        RULE_NOT_FOUND,
        CUSTOMER_SET_NOT_FOUND,
        SUBSCRIBER_NOT_FOUND,
        DUPLICATE_RULE,
        DUPLICATE_CUSTOMER_SET,
        OUTSIDE_COORDINATOR,
    )


class CaptiveAccessPolicyError(DomainError):
    """Stable, transport-neutral refusal of a policy change."""


def _error(code: str, message: str, **details: object) -> CaptiveAccessPolicyError:
    return CaptiveAccessPolicyError(
        code=code, message=message, details=details, retryable=False
    )


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaptiveRuleConditions:
    """Optional match conditions; empty means "any"."""

    subscriber_categories: frozenset[SubscriberCategory] | None = None
    reseller_condition: CaptiveResellerCondition = CaptiveResellerCondition.any
    reseller_ids: frozenset[UUID] = frozenset()


@dataclass(frozen=True, slots=True)
class CaptiveAccessRuleView:
    id: UUID
    scope: CaptiveAccessRuleScope
    effect: CaptiveAccessRuleEffect
    conditions: CaptiveRuleConditions
    created_at: datetime
    created_by: str
    reason: str
    enabled: bool = True
    subscriber_id: UUID | None = None
    customer_set_id: UUID | None = None
    plan_family: str | None = None
    offer_ids: frozenset[UUID] = frozenset()


@dataclass(frozen=True, slots=True)
class CaptiveCustomerSetView:
    id: UUID
    name: str
    is_active: bool
    member_ids: frozenset[UUID]


@dataclass(frozen=True, slots=True)
class CaptiveSubscriptionFacts:
    """Authoritative facts a rule can match for one subscription."""

    subscription_id: UUID
    subscriber_id: UUID
    offer_id: UUID | None
    plan_family: str | None
    subscriber_category: SubscriberCategory | None
    reseller_id: UUID | None
    reseller_is_house: bool


@dataclass(frozen=True, slots=True)
class CaptivePolicyResolution:
    effect: CaptiveAccessRuleEffect
    scope: CaptiveAccessRuleScope | None
    rule_id: UUID | None

    @property
    def allows(self) -> bool:
        return self.effect is CaptiveAccessRuleEffect.allow

    @property
    def defaulted(self) -> bool:
        return self.rule_id is None


DEFAULT_DENY = CaptivePolicyResolution(
    effect=CaptiveAccessRuleEffect.deny, scope=None, rule_id=None
)


def _conditions_hold(
    conditions: CaptiveRuleConditions, facts: CaptiveSubscriptionFacts
) -> bool:
    categories = conditions.subscriber_categories
    if categories is not None and facts.subscriber_category not in categories:
        return False
    if conditions.reseller_condition is CaptiveResellerCondition.house:
        return facts.reseller_is_house
    if conditions.reseller_condition is CaptiveResellerCondition.specific:
        return facts.reseller_id is not None and facts.reseller_id in (
            conditions.reseller_ids
        )
    return True


@dataclass(frozen=True, slots=True)
class CaptivePolicySnapshot:
    """Enabled rules and open cohort memberships at one read instant."""

    rules: tuple[CaptiveAccessRuleView, ...]
    customer_sets: Mapping[UUID, CaptiveCustomerSetView] = field(default_factory=dict)

    def _member_set_ids(self, subscriber_id: UUID) -> frozenset[UUID]:
        return frozenset(
            item.id
            for item in self.customer_sets.values()
            if item.is_active and subscriber_id in item.member_ids
        )

    def _target_matches(
        self,
        rule: CaptiveAccessRuleView,
        facts: CaptiveSubscriptionFacts,
        member_set_ids: frozenset[UUID],
    ) -> bool:
        if rule.scope is CaptiveAccessRuleScope.account:
            return rule.subscriber_id == facts.subscriber_id
        if rule.scope is CaptiveAccessRuleScope.customer_set:
            return rule.customer_set_id in member_set_ids
        if rule.scope is CaptiveAccessRuleScope.plan_family:
            if facts.plan_family is None or rule.plan_family != facts.plan_family:
                return False
            return not rule.offer_ids or facts.offer_id in rule.offer_ids
        return True

    def resolve(self, facts: CaptiveSubscriptionFacts) -> CaptivePolicyResolution:
        member_set_ids = self._member_set_ids(facts.subscriber_id)
        for scope in SCOPE_PRECEDENCE:
            matching = sorted(
                (
                    rule
                    for rule in self.rules
                    if rule.enabled
                    and rule.scope is scope
                    and self._target_matches(rule, facts, member_set_ids)
                    and _conditions_hold(rule.conditions, facts)
                ),
                key=lambda rule: (rule.created_at, str(rule.id)),
            )
            if not matching:
                continue
            deny = next(
                (r for r in matching if r.effect is CaptiveAccessRuleEffect.deny),
                None,
            )
            chosen = deny or matching[0]
            return CaptivePolicyResolution(
                effect=chosen.effect, scope=scope, rule_id=chosen.id
            )
        return DEFAULT_DENY


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _uuid_set(values: Sequence[str] | None) -> frozenset[UUID]:
    result: set[UUID] = set()
    for value in values or ():
        try:
            result.add(UUID(str(value)))
        except ValueError:
            # Malformed persisted evidence never widens a rule: an unparsable
            # id simply cannot match.
            continue
    return frozenset(result)


def _category_set(values: Sequence[str] | None) -> frozenset[SubscriberCategory] | None:
    if values is None:
        return None
    result: set[SubscriberCategory] = set()
    for value in values:
        try:
            result.add(SubscriberCategory(str(value)))
        except ValueError:
            continue
    return frozenset(result)


def rule_view(row: CaptiveAccessRule) -> CaptiveAccessRuleView:
    return CaptiveAccessRuleView(
        id=row.id,
        scope=CaptiveAccessRuleScope(row.scope),
        effect=CaptiveAccessRuleEffect(row.effect),
        conditions=CaptiveRuleConditions(
            subscriber_categories=_category_set(row.subscriber_categories),
            reseller_condition=CaptiveResellerCondition(row.reseller_condition),
            reseller_ids=_uuid_set(row.reseller_ids),
        ),
        created_at=_aware(row.created_at),
        created_by=row.created_by,
        reason=row.reason,
        enabled=bool(row.enabled),
        subscriber_id=row.subscriber_id,
        customer_set_id=row.customer_set_id,
        plan_family=row.plan_family,
        offer_ids=_uuid_set(row.offer_ids),
    )


def load_captive_policy_snapshot(db: Session) -> CaptivePolicySnapshot:
    """Read enabled rules and open memberships of active sets (read-only)."""

    rules = tuple(
        rule_view(row)
        for row in db.scalars(
            select(CaptiveAccessRule)
            .where(CaptiveAccessRule.enabled.is_(True))
            .order_by(CaptiveAccessRule.created_at, CaptiveAccessRule.id)
        ).all()
    )
    members: dict[UUID, set[UUID]] = {}
    for set_id, subscriber_id in db.execute(
        select(
            CaptiveCustomerSetMember.customer_set_id,
            CaptiveCustomerSetMember.subscriber_id,
        ).where(CaptiveCustomerSetMember.removed_at.is_(None))
    ).all():
        members.setdefault(set_id, set()).add(subscriber_id)
    sets = {
        row.id: CaptiveCustomerSetView(
            id=row.id,
            name=row.name,
            is_active=bool(row.is_active),
            member_ids=frozenset(members.get(row.id, ())),
        )
        for row in db.scalars(select(CaptiveCustomerSet)).all()
    }
    return CaptivePolicySnapshot(rules=rules, customer_sets=sets)


def explicit_subscriber_category(account: Subscriber) -> SubscriberCategory | None:
    """Explicit category evidence only; unclassified accounts are ``None``."""

    raw = (account.metadata_ or {}).get("subscriber_category")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return SubscriberCategory(raw.strip().lower())
    except ValueError:
        return None


def subscription_facts(
    db: Session, subscription: Subscription, account: Subscriber
) -> CaptiveSubscriptionFacts:
    reseller = account.reseller
    if reseller is None and account.reseller_id is not None:
        reseller = db.get(Reseller, account.reseller_id)
    offer = subscription.offer
    plan_family = None
    if offer is not None and offer.plan_family:
        plan_family = str(offer.plan_family).strip().lower() or None
    return CaptiveSubscriptionFacts(
        subscription_id=subscription.id,
        subscriber_id=account.id,
        offer_id=subscription.offer_id,
        plan_family=plan_family,
        subscriber_category=explicit_subscriber_category(account),
        reseller_id=reseller.id if reseller is not None else None,
        reseller_is_house=bool(
            reseller is not None and reseller.is_active and reseller.is_house
        ),
    )


# ---------------------------------------------------------------------------
# Typed changes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaptiveRuleSpec:
    scope: CaptiveAccessRuleScope
    effect: CaptiveAccessRuleEffect
    reason: str
    subscriber_id: UUID | None = None
    customer_set_id: UUID | None = None
    plan_family: str | None = None
    offer_ids: frozenset[UUID] = frozenset()
    conditions: CaptiveRuleConditions = CaptiveRuleConditions()


@dataclass(frozen=True, slots=True)
class ReevaluateCaptivePolicy:
    """No rule change: re-evaluate existing locks against current policy."""


@dataclass(frozen=True, slots=True)
class AddCaptiveAccessRule:
    rule: CaptiveRuleSpec


@dataclass(frozen=True, slots=True)
class DisableCaptiveAccessRule:
    rule_id: UUID
    reason: str


@dataclass(frozen=True, slots=True)
class CreateCaptiveCustomerSet:
    name: str
    reason: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class AddCaptiveCustomerSetMembers:
    customer_set_id: UUID
    subscriber_ids: frozenset[UUID]
    reason: str


@dataclass(frozen=True, slots=True)
class RemoveCaptiveCustomerSetMembers:
    customer_set_id: UUID
    subscriber_ids: frozenset[UUID]
    reason: str


CaptivePolicyChange = (
    ReevaluateCaptivePolicy
    | AddCaptiveAccessRule
    | DisableCaptiveAccessRule
    | CreateCaptiveCustomerSet
    | AddCaptiveCustomerSetMembers
    | RemoveCaptiveCustomerSetMembers
)


def change_kind(change: CaptivePolicyChange) -> str:
    return {
        ReevaluateCaptivePolicy: "reevaluate",
        AddCaptiveAccessRule: "add_rule",
        DisableCaptiveAccessRule: "disable_rule",
        CreateCaptiveCustomerSet: "create_customer_set",
        AddCaptiveCustomerSetMembers: "add_customer_set_members",
        RemoveCaptiveCustomerSetMembers: "remove_customer_set_members",
    }[type(change)]


def _sorted_ids(values: Iterable[UUID]) -> list[str]:
    return sorted(str(value) for value in values)


def _conditions_payload(conditions: CaptiveRuleConditions) -> dict[str, object]:
    return {
        "subscriber_categories": (
            sorted(item.value for item in conditions.subscriber_categories)
            if conditions.subscriber_categories is not None
            else None
        ),
        "reseller_condition": conditions.reseller_condition.value,
        "reseller_ids": _sorted_ids(conditions.reseller_ids),
    }


def change_payload(change: CaptivePolicyChange) -> dict[str, object]:
    """Canonical, JSON-safe serialization used for fingerprints and audit."""

    payload: dict[str, object] = {"kind": change_kind(change)}
    if isinstance(change, AddCaptiveAccessRule):
        rule = change.rule
        payload.update(
            scope=rule.scope.value,
            effect=rule.effect.value,
            subscriber_id=str(rule.subscriber_id) if rule.subscriber_id else None,
            customer_set_id=(
                str(rule.customer_set_id) if rule.customer_set_id else None
            ),
            plan_family=rule.plan_family,
            offer_ids=_sorted_ids(rule.offer_ids),
            conditions=_conditions_payload(rule.conditions),
            reason=rule.reason.strip(),
        )
    elif isinstance(change, DisableCaptiveAccessRule):
        payload.update(rule_id=str(change.rule_id), reason=change.reason.strip())
    elif isinstance(change, CreateCaptiveCustomerSet):
        payload.update(
            name=change.name.strip(),
            description=(change.description or "").strip() or None,
            reason=change.reason.strip(),
        )
    elif isinstance(
        change, AddCaptiveCustomerSetMembers | RemoveCaptiveCustomerSetMembers
    ):
        payload.update(
            customer_set_id=str(change.customer_set_id),
            subscriber_ids=_sorted_ids(change.subscriber_ids),
            reason=change.reason.strip(),
        )
    return payload


def change_fingerprint(change: CaptivePolicyChange) -> str:
    encoded = json.dumps(change_payload(change), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_reason(value: str, *, field_name: str) -> str:
    reason = value.strip()
    if not _MIN_REASON <= len(reason) <= _MAX_REASON:
        raise _error(
            CaptiveAccessPolicyErrorCode.INVALID_CHANGE,
            f"A reason of {_MIN_REASON}-{_MAX_REASON} characters is required.",
            field=field_name,
        )
    return reason


def _validate_rule_spec(
    db: Session, snapshot: CaptivePolicySnapshot, rule: CaptiveRuleSpec
) -> CaptiveRuleSpec:
    _require_reason(rule.reason, field_name="reason")
    targets = {
        CaptiveAccessRuleScope.account: rule.subscriber_id is not None,
        CaptiveAccessRuleScope.customer_set: rule.customer_set_id is not None,
        CaptiveAccessRuleScope.plan_family: bool(rule.plan_family),
    }
    for scope, present in targets.items():
        if present != (rule.scope is scope):
            raise _error(
                CaptiveAccessPolicyErrorCode.INVALID_CHANGE,
                "Rule target fields must match the rule scope exactly.",
                scope=rule.scope.value,
            )
    if rule.offer_ids and rule.scope is not CaptiveAccessRuleScope.plan_family:
        raise _error(
            CaptiveAccessPolicyErrorCode.INVALID_CHANGE,
            "Offer ids may only narrow a plan_family rule.",
        )
    conditions = rule.conditions
    if (conditions.reseller_condition is CaptiveResellerCondition.specific) != bool(
        conditions.reseller_ids
    ):
        raise _error(
            CaptiveAccessPolicyErrorCode.INVALID_CHANGE,
            "Specific reseller ids are required exactly when the reseller "
            "condition is 'specific'.",
        )
    if conditions.subscriber_categories is not None and not (
        conditions.subscriber_categories
    ):
        raise _error(
            CaptiveAccessPolicyErrorCode.INVALID_CHANGE,
            "An empty category condition would never match; omit it instead.",
        )
    plan_family = rule.plan_family
    if plan_family is not None:
        plan_family = plan_family.strip().lower()
        if not plan_family or len(plan_family) > 40:
            raise _error(
                CaptiveAccessPolicyErrorCode.INVALID_CHANGE,
                "Plan family must be 1-40 characters.",
            )
    if rule.subscriber_id is not None and db.get(Subscriber, rule.subscriber_id) is (
        None
    ):
        raise _error(
            CaptiveAccessPolicyErrorCode.SUBSCRIBER_NOT_FOUND,
            "The account named by the rule does not exist.",
        )
    if rule.customer_set_id is not None:
        target_set = snapshot.customer_sets.get(rule.customer_set_id)
        if target_set is None or not target_set.is_active:
            raise _error(
                CaptiveAccessPolicyErrorCode.CUSTOMER_SET_NOT_FOUND,
                "The customer set named by the rule does not exist or is inactive.",
            )
    if conditions.reseller_ids:
        found = set(
            db.scalars(
                select(Reseller.id).where(
                    Reseller.id.in_(sorted(conditions.reseller_ids))
                )
            ).all()
        )
        if found != set(conditions.reseller_ids):
            raise _error(
                CaptiveAccessPolicyErrorCode.INVALID_CHANGE,
                "Every reseller named by the condition must exist.",
            )
    normalized = replace(rule, plan_family=plan_family, reason=rule.reason.strip())
    for existing in snapshot.rules:
        if (
            existing.enabled
            and existing.scope is normalized.scope
            and existing.effect is normalized.effect
            and existing.subscriber_id == normalized.subscriber_id
            and existing.customer_set_id == normalized.customer_set_id
            and existing.plan_family == normalized.plan_family
            and existing.offer_ids == normalized.offer_ids
            and existing.conditions == normalized.conditions
        ):
            raise _error(
                CaptiveAccessPolicyErrorCode.DUPLICATE_RULE,
                "An identical enabled rule already exists.",
                rule_id=str(existing.id),
            )
    return normalized


def _existing_subscriber_ids(db: Session, ids: frozenset[UUID]) -> set[UUID]:
    found: set[UUID] = set()
    ordered = sorted(ids)
    for start in range(0, len(ordered), 1000):
        found.update(
            db.scalars(
                select(Subscriber.id).where(
                    Subscriber.id.in_(ordered[start : start + 1000])
                )
            ).all()
        )
    return found


def project_change(
    db: Session,
    snapshot: CaptivePolicySnapshot,
    change: CaptivePolicyChange,
    *,
    actor: str,
    now: datetime,
) -> CaptivePolicySnapshot:
    """Validate ``change`` and return the snapshot it would produce.

    Read-only. Preview and apply both call this, so they share validation and
    the candidate policy they evaluate.
    """

    if isinstance(change, ReevaluateCaptivePolicy):
        return snapshot
    if isinstance(change, AddCaptiveAccessRule):
        rule = _validate_rule_spec(db, snapshot, change.rule)
        candidate = CaptiveAccessRuleView(
            id=CANDIDATE_RULE_ID,
            scope=rule.scope,
            effect=rule.effect,
            conditions=rule.conditions,
            created_at=now,
            created_by=actor,
            reason=rule.reason,
            subscriber_id=rule.subscriber_id,
            customer_set_id=rule.customer_set_id,
            plan_family=rule.plan_family,
            offer_ids=rule.offer_ids,
        )
        return replace(snapshot, rules=(*snapshot.rules, candidate))
    if isinstance(change, DisableCaptiveAccessRule):
        _require_reason(change.reason, field_name="reason")
        if not any(rule.id == change.rule_id for rule in snapshot.rules):
            raise _error(
                CaptiveAccessPolicyErrorCode.RULE_NOT_FOUND,
                "No enabled rule has that id.",
                rule_id=str(change.rule_id),
            )
        return replace(
            snapshot,
            rules=tuple(rule for rule in snapshot.rules if rule.id != change.rule_id),
        )
    if isinstance(change, CreateCaptiveCustomerSet):
        _require_reason(change.reason, field_name="reason")
        name = change.name.strip()
        if not name or len(name) > 120:
            raise _error(
                CaptiveAccessPolicyErrorCode.INVALID_CHANGE,
                "Customer set name must be 1-120 characters.",
            )
        if any(item.name == name for item in snapshot.customer_sets.values()):
            raise _error(
                CaptiveAccessPolicyErrorCode.DUPLICATE_CUSTOMER_SET,
                "A customer set with that name already exists.",
            )
        sets = dict(snapshot.customer_sets)
        sets[CANDIDATE_SET_ID] = CaptiveCustomerSetView(
            id=CANDIDATE_SET_ID, name=name, is_active=True, member_ids=frozenset()
        )
        return replace(snapshot, customer_sets=sets)
    # Membership changes.
    _require_reason(change.reason, field_name="reason")
    target = snapshot.customer_sets.get(change.customer_set_id)
    if target is None or not target.is_active:
        raise _error(
            CaptiveAccessPolicyErrorCode.CUSTOMER_SET_NOT_FOUND,
            "The customer set does not exist or is inactive.",
        )
    if not change.subscriber_ids or len(change.subscriber_ids) > (
        _MAX_MEMBERS_PER_CHANGE
    ):
        raise _error(
            CaptiveAccessPolicyErrorCode.INVALID_CHANGE,
            f"Name 1-{_MAX_MEMBERS_PER_CHANGE} accounts per membership change.",
        )
    if isinstance(change, AddCaptiveCustomerSetMembers):
        missing = change.subscriber_ids - _existing_subscriber_ids(
            db, change.subscriber_ids
        )
        if missing:
            raise _error(
                CaptiveAccessPolicyErrorCode.SUBSCRIBER_NOT_FOUND,
                "Some named accounts do not exist.",
                missing_count=len(missing),
            )
        members = target.member_ids | change.subscriber_ids
    else:
        members = target.member_ids - change.subscriber_ids
    sets = dict(snapshot.customer_sets)
    sets[target.id] = replace(target, member_ids=frozenset(members))
    return replace(snapshot, customer_sets=sets)


# ---------------------------------------------------------------------------
# Participant writer
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StagedCaptivePolicyChange:
    kind: str
    rule_id: UUID | None = None
    customer_set_id: UUID | None = None
    members_added: int = 0
    members_removed: int = 0
    members_unchanged: int = 0


def _audit(
    db: Session,
    *,
    context: CommandContext,
    action: str,
    entity_type: str,
    entity_id: UUID,
    metadata: dict[str, object],
) -> None:
    """Stage PII-free audit evidence and the policy-changed domain event."""

    actor_kind, _, actor_id = context.actor.partition(":")
    is_staff = actor_kind == "system_user" and bool(actor_id)
    stage_audit_event(
        db,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        actor=AuditActor(
            actor_type=AuditActorType.user if is_staff else AuditActorType.system,
            actor_id=actor_id if is_staff else None,
            label=context.actor,
        ),
        request_id=str(context.correlation_id),
        metadata={**metadata, "command_id": str(context.command_id)},
    )
    emit_event(
        db,
        EventType.captive_access_policy_changed,
        {
            "action": action,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "kind": str(metadata.get("kind", "")),
            "command_id": str(context.command_id),
        },
        actor=context.actor,
    )


def stage_captive_policy_change(
    db: Session,
    change: CaptivePolicyChange,
    *,
    context: CommandContext,
    now: datetime,
) -> StagedCaptivePolicyChange:
    """Write one validated change flush-only inside the coordinator command."""

    if not owner_command_active(db, owner=COORDINATOR):
        raise _error(
            CaptiveAccessPolicyErrorCode.OUTSIDE_COORDINATOR,
            "Captive policy records change only through the policy-change coordinator.",
        )
    snapshot = load_captive_policy_snapshot(db)
    # Re-validate against the locked, current snapshot.
    project_change(db, snapshot, change, actor=context.actor, now=now)
    kind = change_kind(change)
    if isinstance(change, ReevaluateCaptivePolicy):
        return StagedCaptivePolicyChange(kind=kind)
    if isinstance(change, AddCaptiveAccessRule):
        spec = _validate_rule_spec(db, snapshot, change.rule)
        categories = spec.conditions.subscriber_categories
        row = CaptiveAccessRule(
            scope=spec.scope.value,
            effect=spec.effect.value,
            subscriber_id=spec.subscriber_id,
            customer_set_id=spec.customer_set_id,
            plan_family=spec.plan_family,
            offer_ids=_sorted_ids(spec.offer_ids) or None,
            subscriber_categories=(
                sorted(item.value for item in categories)
                if categories is not None
                else None
            ),
            reseller_condition=spec.conditions.reseller_condition.value,
            reseller_ids=_sorted_ids(spec.conditions.reseller_ids) or None,
            enabled=True,
            created_by=context.actor,
            reason=spec.reason,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.flush()
        _audit(
            db,
            context=context,
            action="access.captive_access_rule_created",
            entity_type="captive_access_rule",
            entity_id=row.id,
            metadata=change_payload(change),
        )
        return StagedCaptivePolicyChange(kind=kind, rule_id=row.id)
    if isinstance(change, DisableCaptiveAccessRule):
        rule_row = db.scalar(
            select(CaptiveAccessRule)
            .where(CaptiveAccessRule.id == change.rule_id)
            .with_for_update()
        )
        if rule_row is None or not rule_row.enabled:
            raise _error(
                CaptiveAccessPolicyErrorCode.RULE_NOT_FOUND,
                "No enabled rule has that id.",
                rule_id=str(change.rule_id),
            )
        rule_row.enabled = False
        rule_row.disabled_at = now
        rule_row.disabled_by = context.actor
        rule_row.disabled_reason = change.reason.strip()
        rule_row.updated_at = now
        db.flush()
        _audit(
            db,
            context=context,
            action="access.captive_access_rule_disabled",
            entity_type="captive_access_rule",
            entity_id=rule_row.id,
            metadata=change_payload(change),
        )
        return StagedCaptivePolicyChange(kind=kind, rule_id=rule_row.id)
    if isinstance(change, CreateCaptiveCustomerSet):
        set_row = CaptiveCustomerSet(
            name=change.name.strip(),
            description=(change.description or "").strip() or None,
            is_active=True,
            created_by=context.actor,
            reason=change.reason.strip(),
            created_at=now,
            updated_at=now,
        )
        db.add(set_row)
        db.flush()
        _audit(
            db,
            context=context,
            action="access.captive_customer_set_created",
            entity_type="captive_customer_set",
            entity_id=set_row.id,
            metadata=change_payload(change),
        )
        return StagedCaptivePolicyChange(kind=kind, customer_set_id=set_row.id)

    # Membership changes: lock the set row to serialize concurrent editors.
    locked_set = db.scalar(
        select(CaptiveCustomerSet)
        .where(CaptiveCustomerSet.id == change.customer_set_id)
        .with_for_update()
    )
    if locked_set is None or not locked_set.is_active:
        raise _error(
            CaptiveAccessPolicyErrorCode.CUSTOMER_SET_NOT_FOUND,
            "The customer set does not exist or is inactive.",
        )
    open_rows = {
        row.subscriber_id: row
        for row in db.scalars(
            select(CaptiveCustomerSetMember)
            .where(CaptiveCustomerSetMember.customer_set_id == locked_set.id)
            .where(CaptiveCustomerSetMember.removed_at.is_(None))
            .with_for_update()
        ).all()
    }
    reason = change.reason.strip()
    added = removed = unchanged = 0
    if isinstance(change, AddCaptiveCustomerSetMembers):
        for subscriber_id in sorted(change.subscriber_ids):
            if subscriber_id in open_rows:
                unchanged += 1
                continue
            db.add(
                CaptiveCustomerSetMember(
                    customer_set_id=locked_set.id,
                    subscriber_id=subscriber_id,
                    added_by=context.actor,
                    added_reason=reason,
                    added_at=now,
                )
            )
            added += 1
        action = "access.captive_customer_set_members_added"
    else:
        for subscriber_id in sorted(change.subscriber_ids):
            member = open_rows.get(subscriber_id)
            if member is None:
                unchanged += 1
                continue
            member.removed_at = now
            member.removed_by = context.actor
            member.removed_reason = reason
            removed += 1
        action = "access.captive_customer_set_members_removed"
    locked_set.updated_at = now
    db.flush()
    _audit(
        db,
        context=context,
        action=action,
        entity_type="captive_customer_set",
        entity_id=locked_set.id,
        metadata={
            "kind": kind,
            "added": added,
            "removed": removed,
            "unchanged": unchanged,
            "reason": reason,
            "subscriber_count": len(change.subscriber_ids),
        },
    )
    return StagedCaptivePolicyChange(
        kind=kind,
        customer_set_id=locked_set.id,
        members_added=added,
        members_removed=removed,
        members_unchanged=unchanged,
    )


# ---------------------------------------------------------------------------
# Read queries for operator adapters
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaptiveAccessRuleListQuery:
    include_disabled: bool = False


@dataclass(frozen=True, slots=True)
class CaptiveAccessRuleListing:
    rules: tuple[CaptiveAccessRuleView, ...]
    customer_sets: tuple[CaptiveCustomerSetView, ...]


def list_captive_access_rules(
    db: Session, *, query: CaptiveAccessRuleListQuery
) -> CaptiveAccessRuleListing:
    statement = select(CaptiveAccessRule).order_by(
        CaptiveAccessRule.created_at, CaptiveAccessRule.id
    )
    if not query.include_disabled:
        statement = statement.where(CaptiveAccessRule.enabled.is_(True))
    snapshot = load_captive_policy_snapshot(db)
    return CaptiveAccessRuleListing(
        rules=tuple(rule_view(row) for row in db.scalars(statement).all()),
        customer_sets=tuple(
            sorted(snapshot.customer_sets.values(), key=lambda item: item.name)
        ),
    )


__all__ = [
    "CANDIDATE_RULE_ID",
    "DEFAULT_DENY",
    "SCOPE_PRECEDENCE",
    "AddCaptiveAccessRule",
    "AddCaptiveCustomerSetMembers",
    "CaptiveAccessPolicyError",
    "CaptiveAccessPolicyErrorCode",
    "CaptiveAccessRuleListQuery",
    "CaptiveAccessRuleListing",
    "CaptiveAccessRuleView",
    "CaptiveCustomerSetView",
    "CaptivePolicyChange",
    "CaptivePolicyResolution",
    "CaptivePolicySnapshot",
    "CaptiveRuleConditions",
    "CaptiveRuleSpec",
    "CaptiveSubscriptionFacts",
    "CreateCaptiveCustomerSet",
    "DisableCaptiveAccessRule",
    "ReevaluateCaptivePolicy",
    "RemoveCaptiveCustomerSetMembers",
    "StagedCaptivePolicyChange",
    "change_fingerprint",
    "change_kind",
    "change_payload",
    "explicit_subscriber_category",
    "list_captive_access_rules",
    "load_captive_policy_snapshot",
    "project_change",
    "rule_view",
    "stage_captive_policy_change",
    "subscription_facts",
]
