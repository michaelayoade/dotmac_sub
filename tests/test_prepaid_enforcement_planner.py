"""Prepaid enforcement planning is exact, read-only, and shared by the sweep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.notification import Notification
from app.models.subscriber import SubscriberStatus
from app.services.access_resolution import PrepaidFundingDecision
from app.services.collections.prepaid_balance_sweep import run_prepaid_balance_sweep
from app.services.prepaid_enforcement_planner import (
    PrepaidEnforcementAction,
    PrepaidFundingSnapshot,
    plan_prepaid_enforcement,
)

_MONDAY_NOON = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


def _prepare(db, account, subscription) -> None:
    account.billing_mode = BillingMode.prepaid
    account.min_balance = Decimal("100.00")
    account.splynx_customer_id = None
    account.deposit = None
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    db.commit()


def _enable(db) -> None:
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
                domain=SettingDomain.collections,
                key="prepaid_enforcement_activation_at",
                value_type=SettingValueType.string,
                value_text=(_MONDAY_NOON - timedelta(days=10)).isoformat(),
                is_active=True,
            ),
        ]
    )
    db.commit()


def test_disabled_control_still_reports_warn_without_writes(
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
        "prepaid_enforcement_activation_at_not_configured"
    )
    assert plan.action_counts == {"warn": 1}
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
    assert item.account_status == "new"
    assert item.derived_account_status == "active"
    assert item.account_status_drift is True


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
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.active


def test_plan_uses_independent_funding_snapshot_without_local_money_fallback(
    db_session, subscriber_account, subscription, monkeypatch
):
    _prepare(db_session, subscriber_account, subscription)
    monkeypatch.setattr(
        "app.services.prepaid_enforcement_planner.resolve_prepaid_funding",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("local funding resolver must not run")
        ),
    )
    snapshot = PrepaidFundingSnapshot(
        captured_at=_MONDAY_NOON,
        source="splynx-cutover-plus-native-events:prod-2026-07-14",
        decisions=(
            PrepaidFundingDecision(
                account_id=str(subscriber_account.id),
                available_balance=Decimal("500.00"),
                required_balance=Decimal("100.00"),
            ),
        ),
    )

    plan = plan_prepaid_enforcement(
        db_session,
        funding_snapshot=snapshot,
        activation_at=_MONDAY_NOON,
    )

    assert plan.generated_at == _MONDAY_NOON
    assert plan.funding_source == snapshot.source
    assert plan.funding_snapshot_at == _MONDAY_NOON
    assert plan.items[0].available_balance == Decimal("500.00")
    assert plan.items[0].action == PrepaidEnforcementAction.ok


def test_plan_rejects_incomplete_independent_funding_snapshot(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    snapshot = PrepaidFundingSnapshot(
        captured_at=_MONDAY_NOON,
        source="cutover-reconstruction",
        decisions=(
            PrepaidFundingDecision(
                account_id="f7a996e4-8a25-4c33-9d73-e69da71cf406",
                available_balance=Decimal("0.00"),
                required_balance=Decimal("100.00"),
            ),
        ),
    )

    with pytest.raises(ValueError, match="missing selected account"):
        plan_prepaid_enforcement(
            db_session,
            account_ids=[subscriber_account.id],
            funding_snapshot=snapshot,
        )


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


def test_sweep_suppresses_notice_during_infrastructure_fault(
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

    assert result["warned"] == 1
    db_session.refresh(subscriber_account)
    assert subscriber_account.prepaid_low_balance_at.replace(tzinfo=UTC) == _MONDAY_NOON
    assert (
        db_session.query(Notification)
        .filter(Notification.event_type == "prepaid_balance_enforcement")
        .count()
        == 0
    )
