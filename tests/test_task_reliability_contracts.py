from __future__ import annotations

import app.tasks  # noqa: F401  (import side effect: registers task modules)
from app.celery_app import celery_app
from app.services.task_reliability import (
    TASK_RELIABILITY_CONTRACTS,
    FailureVisibility,
    Idempotency,
    RetryPolicy,
    find_missing_task_reliability_contracts,
    find_stale_task_reliability_contracts,
    is_first_party_task,
)


def _first_party_registered_tasks() -> set[str]:
    return {name for name in celery_app.tasks.keys() if is_first_party_task(name)}


def test_every_first_party_celery_task_has_reliability_contract():
    missing = find_missing_task_reliability_contracts(celery_app.tasks.keys())

    assert not missing, (
        "Every first-party Celery task needs a reliability contract in "
        "app.services.task_reliability.TASK_RELIABILITY_CONTRACTS. Missing: "
        f"{missing}"
    )


def test_task_reliability_contracts_do_not_reference_removed_tasks():
    stale = find_stale_task_reliability_contracts(celery_app.tasks.keys())

    assert not stale, (
        "Reliability contracts reference tasks that are not registered with Celery: "
        f"{stale}"
    )


def test_task_reliability_contracts_are_structured():
    registered = _first_party_registered_tasks()

    assert registered
    for task_name in registered:
        contract = TASK_RELIABILITY_CONTRACTS[task_name]
        assert contract.domain
        assert isinstance(contract.retry_policy, RetryPolicy)
        assert isinstance(contract.idempotency, Idempotency)
        assert isinstance(contract.failure_visibility, FailureVisibility)


def test_money_moving_tasks_do_not_use_blind_celery_autoretry():
    money_moving_tasks = {
        "app.tasks.autopay.charge_due_invoices",
        "app.tasks.billing.run_invoice_cycle",
        "app.tasks.collections.run_billing_enforcement",
        "app.tasks.collections.run_dunning",
        "app.tasks.payment_reconciliation.reconcile_topups",
        "app.tasks.usage.meter_usage_into_quota",
        "app.tasks.usage.run_usage_rating",
        "app.tasks.vas.run_wallet_auto_deduct",
    }

    for task_name in money_moving_tasks:
        contract = TASK_RELIABILITY_CONTRACTS[task_name]
        assert contract.retry_policy is not RetryPolicy.CELERY_AUTORETRY
        assert contract.idempotency is not Idempotency.NON_IDEMPOTENT
