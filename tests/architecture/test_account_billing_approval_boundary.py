"""Billing approval is an admission/lifecycle command, never a loose flag."""

from pathlib import Path

from app.services.scheduler import PERMANENT_CUSTOMER_LIFECYCLE_TASKS
from app.services.task_reliability import (
    TASK_RELIABILITY_CONTRACTS,
    FailureVisibility,
    Idempotency,
    RetryPolicy,
)

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_profile_and_bulk_adapters_do_not_write_billing_approval() -> None:
    source = _source("app/services/web_customer_actions.py")
    assert "subscriber.billing_enabled =" not in source
    assert "change_account_billing_approval(" in source


def test_generic_subscriber_update_rejects_billing_approval_mutation() -> None:
    source = _source("app/services/subscriber.py")
    assert source.count('data.pop("billing_enabled", None)') >= 2
    assert "Billing approval is a lifecycle command" in source


def test_active_transitions_require_billing_approval() -> None:
    source = _source("app/services/account_lifecycle.py")
    assert "def _require_billing_approval(" in source
    assert source.count("_require_billing_approval(") >= 5


def test_drift_reconciler_is_permanent_and_not_flag_gated() -> None:
    task_name = "app.tasks.enforcement.reconcile_billing_approval_drift"
    assert task_name in PERMANENT_CUSTOMER_LIFECYCLE_TASKS
    scheduler = _source("app/services/scheduler_config.py")
    task_index = scheduler.index(f'task_name="{task_name}"')
    block = scheduler[task_index - 200 : task_index + 200]
    assert "enabled=True" in block


def test_drift_reconciler_retries_by_guarded_per_item_beat_pass() -> None:
    contract = TASK_RELIABILITY_CONTRACTS[
        "app.tasks.enforcement.reconcile_billing_approval_drift"
    ]

    assert contract.retry_policy is RetryPolicy.BEAT_RERUN
    assert contract.idempotency is Idempotency.PER_ITEM_GUARDED
    assert contract.failure_visibility is FailureVisibility.LOG_ONLY


def test_billing_approval_owner_is_the_only_explicit_runtime_field_writer() -> None:
    owner = _source("app/services/account_billing_approval.py")
    assert "account.billing_enabled = False" in owner
    assert "account.billing_enabled = True" in owner
    for relative in (
        "app/services/web_customer_actions.py",
        "app/services/subscriber.py",
        "app/services/account_lifecycle.py",
    ):
        source = _source(relative)
        assert ".billing_enabled = False" not in source
        assert ".billing_enabled = True" not in source


def test_retired_global_billing_switch_is_not_a_presentation_fallback() -> None:
    for relative in (
        "app/services/web_customer_details.py",
        "app/services/web_catalog_subscriptions.py",
    ):
        source = _source(relative)
        defaults = source[source.index("def _billing_global_defaults") :]
        defaults = defaults[: defaults.index("\n\ndef ", 1)]
        assert '"billing_enabled"' not in defaults
