"""Guards for the one operationally-current subscription decision boundary."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_service_health_has_one_shared_operational_cohort_owner():
    policy = _source("app/services/subscription_lifecycle_policy.py")
    service_status = _source("app/services/service_status.py")
    account_health = _source("app/services/portal_account_health.py")

    assert "HISTORICAL_WHEN_ENDED_SERVICE_STATUSES" in policy
    assert "operationally_current_subscription_filters" in policy
    assert "_CURRENT_STATUSES" not in service_status
    assert "resolve_operational_subscription_cohort" in service_status
    assert "Subscription.status.in_" not in account_health
    assert "resolve_operational_subscription_cohort" in account_health
    assert "cohort=cohort" in account_health


def test_operational_cohort_remains_a_read_only_projection():
    policy = _source("app/services/subscription_lifecycle_policy.py")
    service_status = _source("app/services/service_status.py")

    assert ".commit(" not in policy
    assert ".rollback(" not in policy
    assert "execute_subscription_command" not in service_status
    assert "subscriptions.status" not in service_status
