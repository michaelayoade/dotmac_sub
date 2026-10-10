"""Resolution precedence and validation of the composable captive policy.

The resolution tests are pure (typed snapshot in, typed resolution out). The
migration tests read the revision source (fast lane); the real backfill runs
on PostgreSQL in ``tests/integration/test_captive_access_policy_migration.py``.
"""

from __future__ import annotations

import importlib.util
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.models.captive_access_policy import (
    CaptiveAccessRuleEffect,
    CaptiveAccessRuleScope,
    CaptiveResellerCondition,
)
from app.models.subscriber import SubscriberCategory
from app.services.captive_access_policy import (
    AddCaptiveAccessRule,
    AddCaptiveCustomerSetMembers,
    CaptiveAccessPolicyError,
    CaptiveAccessPolicyErrorCode,
    CaptiveAccessRuleView,
    CaptiveCustomerSetView,
    CaptivePolicySnapshot,
    CaptiveRuleConditions,
    CaptiveRuleSpec,
    CaptiveSubscriptionFacts,
    CreateCaptiveCustomerSet,
    DisableCaptiveAccessRule,
    ReevaluateCaptivePolicy,
    change_fingerprint,
    load_captive_policy_snapshot,
    project_change,
    stage_captive_policy_change,
)
from app.services.owner_commands import CommandContext
from tests.captive_access_support import add_rule, customer_set

T0 = datetime(2026, 10, 1, tzinfo=UTC)
SUBSCRIBER = uuid4()
SUBSCRIPTION = uuid4()
OFFER = uuid4()
HOUSE = uuid4()
OTHER_RESELLER = uuid4()

Scope = CaptiveAccessRuleScope
Effect = CaptiveAccessRuleEffect


def _facts(**overrides: object) -> CaptiveSubscriptionFacts:
    values: dict[str, object] = {
        "subscription_id": SUBSCRIPTION,
        "subscriber_id": SUBSCRIBER,
        "offer_id": OFFER,
        "plan_family": "home_flex",
        "subscriber_category": SubscriberCategory.residential,
        "reseller_id": HOUSE,
        "reseller_is_house": True,
    }
    values.update(overrides)
    return CaptiveSubscriptionFacts(**values)  # type: ignore[arg-type]


def _rule(
    scope: CaptiveAccessRuleScope,
    effect: CaptiveAccessRuleEffect = Effect.allow,
    *,
    minutes: int = 0,
    conditions: CaptiveRuleConditions | None = None,
    **targets: object,
) -> CaptiveAccessRuleView:
    return CaptiveAccessRuleView(
        id=uuid4(),
        scope=scope,
        effect=effect,
        conditions=conditions or CaptiveRuleConditions(),
        created_at=T0 + timedelta(minutes=minutes),
        created_by="test",
        reason="test",
        **targets,  # type: ignore[arg-type]
    )


def _snapshot(
    *rules: CaptiveAccessRuleView,
    sets: dict[UUID, CaptiveCustomerSetView] | None = None,
) -> CaptivePolicySnapshot:
    return CaptivePolicySnapshot(rules=rules, customer_sets=sets or {})


def _cohort(*members: UUID, active: bool = True) -> CaptiveCustomerSetView:
    return CaptiveCustomerSetView(
        id=uuid4(),
        name=f"set-{uuid4().hex[:4]}",
        is_active=active,
        member_ids=frozenset(members),
    )


# -- precedence --------------------------------------------------------------


def test_no_rule_is_default_deny() -> None:
    resolution = _snapshot().resolve(_facts())

    assert not resolution.allows
    assert resolution.defaulted
    assert resolution.scope is None


def test_global_allow_applies_to_every_subscription() -> None:
    rule = _rule(Scope.global_)
    resolution = _snapshot(rule).resolve(_facts())

    assert resolution.allows
    assert resolution.scope is Scope.global_
    assert resolution.rule_id == rule.id


def test_each_more_specific_scope_overrides_the_less_specific() -> None:
    cohort = _cohort(SUBSCRIBER)
    global_allow = _rule(Scope.global_)
    family_deny = _rule(Scope.plan_family, Effect.deny, plan_family="home_flex")
    set_allow = _rule(Scope.customer_set, customer_set_id=cohort.id)
    account_deny = _rule(Scope.account, Effect.deny, subscriber_id=SUBSCRIBER)
    sets = {cohort.id: cohort}

    assert _snapshot(global_allow).resolve(_facts()).allows
    assert not _snapshot(global_allow, family_deny).resolve(_facts()).allows
    resolved = _snapshot(global_allow, family_deny, set_allow, sets=sets).resolve(
        _facts()
    )
    assert resolved.allows and resolved.scope is Scope.customer_set
    resolved = _snapshot(
        global_allow, family_deny, set_allow, account_deny, sets=sets
    ).resolve(_facts())
    assert not resolved.allows and resolved.scope is Scope.account


def test_account_allow_beats_global_deny() -> None:
    resolution = _snapshot(
        _rule(Scope.global_, Effect.deny),
        _rule(Scope.account, subscriber_id=SUBSCRIBER),
    ).resolve(_facts())

    assert resolution.allows
    assert resolution.scope is Scope.account


@pytest.mark.parametrize(
    "scope,targets",
    [
        (Scope.global_, {}),
        (Scope.plan_family, {"plan_family": "home_flex"}),
        (Scope.account, {"subscriber_id": SUBSCRIBER}),
    ],
)
def test_deny_beats_allow_at_the_same_scope(
    scope: CaptiveAccessRuleScope, targets: dict[str, object]
) -> None:
    allow = _rule(scope, minutes=0, **targets)
    deny = _rule(scope, Effect.deny, minutes=5, **targets)

    resolution = _snapshot(allow, deny).resolve(_facts())

    assert not resolution.allows
    assert resolution.rule_id == deny.id


def test_deny_beats_allow_at_customer_set_scope() -> None:
    first, second = _cohort(SUBSCRIBER), _cohort(SUBSCRIBER)
    resolution = _snapshot(
        _rule(Scope.customer_set, customer_set_id=first.id),
        _rule(Scope.customer_set, Effect.deny, customer_set_id=second.id),
        sets={first.id: first, second.id: second},
    ).resolve(_facts())

    assert not resolution.allows


def test_plan_family_rule_narrowed_to_offers() -> None:
    rule = _rule(
        Scope.plan_family, plan_family="home_flex", offer_ids=frozenset({uuid4()})
    )

    assert _snapshot(rule).resolve(_facts()).defaulted
    rule = _rule(
        Scope.plan_family, plan_family="home_flex", offer_ids=frozenset({OFFER})
    )
    assert _snapshot(rule).resolve(_facts()).allows
    assert _snapshot(rule).resolve(_facts(plan_family="unlimited")).defaulted
    assert _snapshot(rule).resolve(_facts(plan_family=None)).defaulted


def test_inactive_customer_set_and_non_members_do_not_match() -> None:
    inactive = _cohort(SUBSCRIBER, active=False)
    other = _cohort(uuid4())
    snapshot = _snapshot(
        _rule(Scope.customer_set, customer_set_id=inactive.id),
        _rule(Scope.customer_set, customer_set_id=other.id),
        sets={inactive.id: inactive, other.id: other},
    )

    assert snapshot.resolve(_facts()).defaulted


def test_category_condition_and_unclassified_accounts() -> None:
    residential_only = CaptiveRuleConditions(
        subscriber_categories=frozenset({SubscriberCategory.residential})
    )
    rule = _rule(Scope.global_, conditions=residential_only)

    assert _snapshot(rule).resolve(_facts()).allows
    assert (
        _snapshot(rule)
        .resolve(_facts(subscriber_category=SubscriberCategory.business))
        .defaulted
    )
    assert _snapshot(rule).resolve(_facts(subscriber_category=None)).defaulted
    # No category condition: business and unclassified match deliberately.
    assert (
        _snapshot(_rule(Scope.global_))
        .resolve(_facts(subscriber_category=SubscriberCategory.business))
        .allows
    )


def test_reseller_conditions() -> None:
    house_only = _rule(
        Scope.global_,
        conditions=CaptiveRuleConditions(
            reseller_condition=CaptiveResellerCondition.house
        ),
    )
    specific = _rule(
        Scope.global_,
        conditions=CaptiveRuleConditions(
            reseller_condition=CaptiveResellerCondition.specific,
            reseller_ids=frozenset({OTHER_RESELLER}),
        ),
    )

    assert _snapshot(house_only).resolve(_facts()).allows
    assert (
        _snapshot(house_only)
        .resolve(_facts(reseller_is_house=False, reseller_id=OTHER_RESELLER))
        .defaulted
    )
    assert (
        _snapshot(specific)
        .resolve(_facts(reseller_is_house=False, reseller_id=OTHER_RESELLER))
        .allows
    )
    assert _snapshot(specific).resolve(_facts()).defaulted
    assert _snapshot(specific).resolve(_facts(reseller_id=None)).defaulted


def test_conditions_failing_at_a_specific_scope_fall_through() -> None:
    business_account_rule = _rule(
        Scope.account,
        Effect.deny,
        subscriber_id=SUBSCRIBER,
        conditions=CaptiveRuleConditions(
            subscriber_categories=frozenset({SubscriberCategory.business})
        ),
    )
    resolution = _snapshot(business_account_rule, _rule(Scope.global_)).resolve(
        _facts()
    )

    assert resolution.allows
    assert resolution.scope is Scope.global_


def test_disabled_rules_never_match() -> None:
    disabled = replace(_rule(Scope.global_), enabled=False)

    assert _snapshot(disabled).resolve(_facts()).defaulted


# -- validation and staging (fast SQLite lane) -------------------------------


def _context() -> CommandContext:
    return CommandContext.system(
        actor="system_user:00000000-0000-0000-0000-000000000001",
        scope="access:captive_access_policy:write",
        reason="unit test",
    )


def test_project_change_validates_scope_targets(db_session, subscriber) -> None:
    snapshot = load_captive_policy_snapshot(db_session)
    bad = AddCaptiveAccessRule(
        rule=CaptiveRuleSpec(
            scope=Scope.account,
            effect=Effect.allow,
            reason="valid reason text",
            plan_family="home_flex",
        )
    )
    with pytest.raises(CaptiveAccessPolicyError) as exc:
        project_change(db_session, snapshot, bad, actor="t", now=T0)
    assert exc.value.code == CaptiveAccessPolicyErrorCode.INVALID_CHANGE

    short_reason = AddCaptiveAccessRule(
        rule=CaptiveRuleSpec(
            scope=Scope.account,
            effect=Effect.allow,
            reason="short",
            subscriber_id=subscriber.id,
        )
    )
    with pytest.raises(CaptiveAccessPolicyError):
        project_change(db_session, snapshot, short_reason, actor="t", now=T0)

    missing = AddCaptiveAccessRule(
        rule=CaptiveRuleSpec(
            scope=Scope.account,
            effect=Effect.allow,
            reason="valid reason text",
            subscriber_id=uuid4(),
        )
    )
    with pytest.raises(CaptiveAccessPolicyError) as exc:
        project_change(db_session, snapshot, missing, actor="t", now=T0)
    assert exc.value.code == CaptiveAccessPolicyErrorCode.SUBSCRIBER_NOT_FOUND


def test_duplicate_enabled_rule_is_refused(db_session, subscriber) -> None:
    add_rule(db_session, scope="account", subscriber_id=subscriber.id)
    duplicate = AddCaptiveAccessRule(
        rule=CaptiveRuleSpec(
            scope=Scope.account,
            effect=Effect.allow,
            reason="valid reason text",
            subscriber_id=subscriber.id,
        )
    )

    with pytest.raises(CaptiveAccessPolicyError) as exc:
        project_change(
            db_session,
            load_captive_policy_snapshot(db_session),
            duplicate,
            actor="t",
            now=T0,
        )
    assert exc.value.code == CaptiveAccessPolicyErrorCode.DUPLICATE_RULE


def test_projected_membership_and_disable_changes(db_session, subscriber) -> None:
    cohort = customer_set(db_session, name="pilot")
    rule = add_rule(db_session, scope="customer_set", customer_set_id=cohort.id)
    snapshot = load_captive_policy_snapshot(db_session)
    facts = _facts(subscriber_id=subscriber.id)

    assert snapshot.resolve(facts).defaulted
    added = project_change(
        db_session,
        snapshot,
        AddCaptiveCustomerSetMembers(
            customer_set_id=cohort.id,
            subscriber_ids=frozenset({subscriber.id}),
            reason="pilot membership",
        ),
        actor="t",
        now=T0,
    )
    assert added.resolve(facts).allows
    disabled = project_change(
        db_session,
        added,
        DisableCaptiveAccessRule(rule_id=rule.id, reason="pilot finished"),
        actor="t",
        now=T0,
    )
    assert disabled.resolve(facts).defaulted
    # Projection never wrote anything.
    assert load_captive_policy_snapshot(db_session).resolve(facts).defaulted


def test_create_set_rejects_duplicate_names(db_session) -> None:
    customer_set(db_session, name="pilot")
    with pytest.raises(CaptiveAccessPolicyError) as exc:
        project_change(
            db_session,
            load_captive_policy_snapshot(db_session),
            CreateCaptiveCustomerSet(name="pilot", reason="another pilot set"),
            actor="t",
            now=T0,
        )
    assert exc.value.code == CaptiveAccessPolicyErrorCode.DUPLICATE_CUSTOMER_SET


def test_participant_writer_refuses_outside_the_coordinator(db_session) -> None:
    with pytest.raises(CaptiveAccessPolicyError) as exc:
        stage_captive_policy_change(
            db_session, ReevaluateCaptivePolicy(), context=_context(), now=T0
        )
    assert exc.value.code == CaptiveAccessPolicyErrorCode.OUTSIDE_COORDINATOR


def test_change_fingerprint_is_order_independent() -> None:
    a, b = uuid4(), uuid4()
    set_id = uuid4()
    first = AddCaptiveCustomerSetMembers(
        customer_set_id=set_id, subscriber_ids=frozenset({a, b}), reason="pilot set"
    )
    second = AddCaptiveCustomerSetMembers(
        customer_set_id=set_id, subscriber_ids=frozenset({b, a}), reason="pilot set "
    )
    assert change_fingerprint(first) == change_fingerprint(second)


# -- migration of existing opt-ins (source contract; PG run is integration) ---

_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _VERSIONS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migrations_chain_expand_then_backfill() -> None:
    expand = _load("665_captive_access_policy_schema")
    backfill = _load("666_captive_access_policy_backfill")

    assert expand.down_revision == "664_purge_retired_splynx_metadata_keys"
    assert backfill.down_revision == expand.revision


def test_backfill_preserves_former_eligibility_and_verifies() -> None:
    source = (_VERSIONS / "666_captive_access_policy_backfill.py").read_text()

    assert "WHERE s.captive_redirect_enabled IS TRUE" in source
    assert "'account', 'allow'" in source
    assert "CAST('[\\\"residential\\\"]' AS JSON), 'house'" in source
    assert "NOT EXISTS" in source  # idempotent re-run
    assert "verification failed" in source
    assert "VALIDATE CONSTRAINT" in source
    # The contract step is NOT in this release.
    assert "DROP COLUMN" not in source.upper().replace("DROP CONSTRAINT", "")
