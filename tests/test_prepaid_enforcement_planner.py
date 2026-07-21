"""Prepaid enforcement planning is exact, read-only, and shared by the sweep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.notification import Notification
from app.models.prepaid_enforcement import PrepaidEnforcementReadiness
from app.models.subscriber import SubscriberStatus
from app.services.collections.prepaid_balance_sweep import run_prepaid_balance_sweep
from app.services.prepaid_enforcement_planner import (
    PrepaidEnforcementAction,
    PrepaidEnforcementError,
    PrepaidEnforcementReasonSource,
    candidate_prepaid_account_ids,
    candidate_prepaid_funding_account_ids,
    plan_prepaid_enforcement,
    resolve_prepaid_enforcement_policy,
)
from tests.prepaid_funding_helpers import materialize_test_prepaid_opening_balance

_MONDAY_NOON = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


def _prepare(db, account, subscription) -> None:
    account.billing_mode = BillingMode.prepaid
    account.min_balance = Decimal("100.00")
    account.splynx_customer_id = None
    account.deposit = None
    account.status = SubscriberStatus.active
    account.is_active = True
    account.billing_enabled = True
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.next_billing_at = None
    db.commit()
    materialize_test_prepaid_opening_balance(db, account.id, Decimal("0.00"))


def _enable(db) -> None:
    activation_at = _MONDAY_NOON - timedelta(days=10)
    db.add_all(
        [
            DomainSetting(
                domain=SettingDomain.modules,
                key="collections_prepaid_balance_enforcement",
                value_type=SettingValueType.boolean,
                value_text="true",
                value_json=True,
                is_active=True,
            ),
            DomainSetting(
                domain=SettingDomain.modules,
                key="billing_prepaid_service_renewals",
                value_type=SettingValueType.boolean,
                value_text="true",
                value_json=True,
                is_active=True,
            ),
            PrepaidEnforcementReadiness(
                intended_activation_at=activation_at,
                funding_observed_at=activation_at,
                source="test-reconciled-funding",
                evidence_ref="test:prepaid-readiness",
                currency="NGN",
                candidate_account_count=1,
                candidate_account_ids_hash="0" * 64,
                configuration_hash="1" * 64,
                funding_decisions_hash="2" * 64,
                reconstruction_evidence_sha256="3" * 64,
                blocker_count=0,
                verified_by="pytest",
                verified_at=activation_at,
                activated_at=activation_at,
                is_active=True,
            ),
        ]
    )
    db.commit()


def test_disabled_control_reports_configured_zero_grace_without_writes(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    notice_count = (
        db_session.query(Notification)
        .filter(Notification.event_type == "prepaid_balance_enforcement")
        .count()
    )

    plan = plan_prepaid_enforcement(
        db_session,
        now=_MONDAY_NOON,
        account_ids=[str(subscriber_account.id)],
    )

    assert plan.control_enabled is False
    assert "deactivation_days" not in plan.policy.report_values()
    assert plan.policy.activation_error == (
        "prepaid_enforcement_readiness_not_recorded"
    )
    assert plan.action_counts == {"suspend": 1}
    assert plan.items[0].available_balance == Decimal("0.00")
    assert plan.items[0].required_balance >= Decimal("100.00")
    db_session.refresh(subscriber_account)
    assert subscriber_account.prepaid_low_balance_at is None
    assert (
        db_session.query(Notification)
        .filter(Notification.event_type == "prepaid_balance_enforcement")
        .count()
        == notice_count
    )


def test_funding_cohort_excludes_postpaid_timer_repair_input(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.postpaid
    subscriber_account.status = SubscriberStatus.active
    subscriber_account.is_active = True
    subscriber_account.billing_enabled = True
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=1)
    subscription.billing_mode = BillingMode.postpaid
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    assert subscriber_account.id in candidate_prepaid_account_ids(db_session)
    assert subscriber_account.id not in candidate_prepaid_funding_account_ids(
        db_session
    )
    item = plan_prepaid_enforcement(
        db_session,
        now=_MONDAY_NOON,
        account_ids=[subscriber_account.id],
    ).items[0]

    assert item.action == PrepaidEnforcementAction.clear_stale_timers
    assert item.reason == "non_prepaid_account_has_prepaid_timers"
    assert item.available_balance == Decimal("0.00")
    assert item.required_balance == Decimal("0.00")


def test_funding_cohort_excludes_service_less_prepaid_timer_repair_input(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.prepaid
    subscriber_account.status = SubscriberStatus.active
    subscriber_account.is_active = True
    subscriber_account.billing_enabled = True
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=1)
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.canceled
    db_session.commit()

    assert subscriber_account.id in candidate_prepaid_account_ids(db_session)
    assert subscriber_account.id not in candidate_prepaid_funding_account_ids(
        db_session
    )
    item = plan_prepaid_enforcement(
        db_session,
        now=_MONDAY_NOON,
        account_ids=[subscriber_account.id],
    ).items[0]

    assert item.action == PrepaidEnforcementAction.clear_stale_timers
    assert item.reason == "account_without_collectible_service_has_prepaid_timers"


def test_plan_reports_parent_status_drift_and_distinct_suspend_action(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    subscriber_account.status = SubscriberStatus.new
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=4)
    db_session.commit()

    item = plan_prepaid_enforcement(
        db_session,
        now=_MONDAY_NOON,
        account_ids=[subscriber_account.id],
    ).items[0]

    assert item.action == PrepaidEnforcementAction.suspend
    assert item.account_status is SubscriberStatus.new
    assert item.derived_account_status is SubscriberStatus.active
    assert item.account_status_drift is True


def test_future_anchor_without_coverage_blocks_adverse_action(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    subscription.next_billing_at = _MONDAY_NOON + timedelta(days=20)
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=4)
    db_session.commit()

    item = plan_prepaid_enforcement(
        db_session,
        now=_MONDAY_NOON,
        account_ids=[subscriber_account.id],
    ).items[0]

    assert item.action == PrepaidEnforcementAction.coverage_unresolved
    assert item.reason_source is PrepaidEnforcementReasonSource.COVERAGE
    assert item.unresolved_projection_subscription_ids == (subscription.id,)


def test_plan_classifies_financial_shield_without_mutation(
    db_session, subscriber_account, subscription, monkeypatch
):
    _prepare(db_session, subscriber_account, subscription)
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=4)
    db_session.commit()
    monkeypatch.setattr(
        "app.services.prepaid_enforcement_planner._bulk_dunning_shield_reasons",
        lambda db, ids: {subscriber_account.id: "payment proof pending review"},
    )

    item = plan_prepaid_enforcement(
        db_session,
        now=_MONDAY_NOON,
        account_ids=[subscriber_account.id],
    ).items[0]

    assert item.action == PrepaidEnforcementAction.shielded
    assert item.reason == "payment proof pending review"
    assert item.reason_source is PrepaidEnforcementReasonSource.SHIELD
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active


def test_invalid_blocking_time_is_a_stable_domain_failure(db_session, monkeypatch):
    from app.services.prepaid_enforcement_planner import settings_spec

    original = settings_spec.resolve_value

    def _setting(db, domain, key):
        if key == "prepaid_blocking_time":
            return "not-a-time"
        return original(db, domain, key)

    monkeypatch.setattr(settings_spec, "resolve_value", _setting)

    with pytest.raises(PrepaidEnforcementError) as captured:
        resolve_prepaid_enforcement_policy(db_session)

    assert captured.value.code == (
        "financial.prepaid_enforcement.invalid_blocking_time"
    )


def test_missing_selected_account_is_a_stable_domain_failure(db_session):
    import uuid

    missing_id = uuid.uuid4()

    with pytest.raises(PrepaidEnforcementError) as captured:
        plan_prepaid_enforcement(db_session, account_ids=[missing_id])

    assert captured.value.code == "financial.prepaid_enforcement.account_not_found"
    assert captured.value.details == {"account_ids": [str(missing_id)]}


def test_plan_always_uses_materialized_funding_owner(
    db_session, subscriber_account, subscription, monkeypatch
):
    _prepare(db_session, subscriber_account, subscription)
    calls: list[str] = []

    def _funding(db, account, *, now):  # noqa: ANN001
        from app.services.access_resolution import PrepaidFundingDecision

        calls.append(str(account.id))
        return PrepaidFundingDecision(
            account_id=str(account.id),
            available_balance=Decimal("500.00"),
            required_balance=Decimal("100.00"),
            currency="NGN",
        )

    monkeypatch.setattr(
        "app.services.prepaid_enforcement_planner.resolve_prepaid_funding",
        _funding,
    )

    plan = plan_prepaid_enforcement(
        db_session,
        now=_MONDAY_NOON,
        account_ids=[subscriber_account.id],
        activation_at=_MONDAY_NOON,
    )

    assert plan.generated_at == _MONDAY_NOON
    assert plan.funding_owner == "financial.prepaid_funding_reconstruction"
    assert plan.funding_observed_at == _MONDAY_NOON
    assert plan.items[0].available_balance == Decimal("500.00")
    assert plan.items[0].action == PrepaidEnforcementAction.ok
    assert calls == [str(subscriber_account.id)]


def test_plan_reports_deactivation_marker_without_prepaid_lock_as_drift(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=2)
    subscriber_account.prepaid_deactivation_at = _MONDAY_NOON - timedelta(days=1)
    db_session.commit()

    item = plan_prepaid_enforcement(
        db_session,
        now=_MONDAY_NOON,
        account_ids=[subscriber_account.id],
    ).items[0]

    assert item.action == PrepaidEnforcementAction.state_drift
    assert item.reason == "deactivation_marker_missing_prepaid_lock"


def test_sweep_does_not_mutate_enforcement_state_drift(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    _enable(db_session)
    subscriber_account.prepaid_low_balance_at = _MONDAY_NOON - timedelta(days=2)
    subscriber_account.prepaid_deactivation_at = _MONDAY_NOON - timedelta(days=1)
    db_session.commit()

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["state_drift"] == 1
    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert subscriber_account.prepaid_deactivation_at is not None
    assert subscription.status == SubscriptionStatus.active


def test_zero_grace_suspends_even_when_notice_is_fault_suppressed(
    db_session, subscriber_account, subscription, monkeypatch
):
    _prepare(db_session, subscriber_account, subscription)
    _enable(db_session)

    def _decisions(db, subscriptions):
        return {
            sub.id: SimpleNamespace(
                suppress_suspension_notice=True,
                reason="open_infrastructure_down_ticket",
            )
            for sub in subscriptions
        }

    monkeypatch.setattr(
        "app.services.prepaid_enforcement_planner.billing_communication_decisions",
        _decisions,
    )

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["suspended"] == 1
    db_session.refresh(subscriber_account)
    assert subscriber_account.prepaid_low_balance_at.replace(tzinfo=UTC) == _MONDAY_NOON
    assert (
        subscriber_account.prepaid_deactivation_at.replace(tzinfo=UTC) == _MONDAY_NOON
    )
    assert (
        db_session.query(Notification)
        .filter(Notification.event_type == "prepaid_balance_enforcement")
        .count()
        == 0
    )


def test_nonzero_grace_does_not_start_until_warning_is_queued(
    db_session, subscriber_account, subscription, monkeypatch
):
    _prepare(db_session, subscriber_account, subscription)
    subscriber_account.grace_period_days = 1
    db_session.commit()
    _enable(db_session)

    def _decisions(db, subscriptions):
        return {
            sub.id: SimpleNamespace(
                suppress_suspension_notice=True,
                reason="open_infrastructure_down_ticket",
            )
            for sub in subscriptions
        }

    monkeypatch.setattr(
        "app.services.prepaid_enforcement_planner.billing_communication_decisions",
        _decisions,
    )

    result = run_prepaid_balance_sweep(db_session, now=_MONDAY_NOON)

    assert result["notice_blocked"] == 1
    assert result["warned"] == 0
    db_session.refresh(subscriber_account)
    assert subscriber_account.prepaid_low_balance_at is None
    assert (
        db_session.query(Notification)
        .filter(Notification.event_type == "prepaid_balance_enforcement")
        .count()
        == 0
    )
