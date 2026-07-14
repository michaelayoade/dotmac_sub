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


# The registry documents a contract; Celery is only the transport that carries
# it out. The tests below keep the two from drifting apart, so a contract cannot
# quietly claim behavior the runtime does not implement.

REDRIVE_POLICIES = {RetryPolicy.MANUAL_REDRIVE, RetryPolicy.DEAD_LETTER_REDRIVE}
OPERATOR_REACHABLE_FAILURES = {
    FailureVisibility.ADMIN_REDRIVE,
    FailureVisibility.DEAD_LETTER,
    FailureVisibility.DOMAIN_STATUS,
}


def test_no_retry_contracts_are_not_autoretried_by_the_transport():
    """A task declared NO_RETRY must not be retried by Celery behind our back.

    max_retries is not a usable signal: Celery defaults it to 3 on every task.
    autoretry_for is what actually arms automatic retries.
    """
    drifted = {
        task_name: getattr(celery_app.tasks[task_name], "autoretry_for", ())
        for task_name in _first_party_registered_tasks()
        if TASK_RELIABILITY_CONTRACTS[task_name].retry_policy is RetryPolicy.NO_RETRY
        and getattr(celery_app.tasks[task_name], "autoretry_for", ())
    }

    assert not drifted, (
        "These tasks declare RetryPolicy.NO_RETRY but Celery is configured to "
        "retry them automatically. Either drop autoretry_for from the task or "
        f"correct its contract: {drifted}"
    )


def test_non_idempotent_redrive_contracts_have_an_operator_reachable_surface():
    """Redriving non-idempotent work needs somewhere an operator can act.

    An idempotent task may claim MANUAL_REDRIVE with log-only visibility: the
    redrive is just running it again, and a repeat is harmless. Non-idempotent
    work is different — a repeat has real-world consequences (re-flashing a
    device, re-issuing a command), so claiming an operator "inspects state and
    redrives" requires a surface that shows that state and guards the duplicate.
    Logs are not that surface, and a contract promising an affordance nothing
    implements is drift rather than policy.
    """
    unreachable = {
        task_name: (
            TASK_RELIABILITY_CONTRACTS[task_name].retry_policy.value,
            TASK_RELIABILITY_CONTRACTS[task_name].failure_visibility.value,
        )
        for task_name in _first_party_registered_tasks()
        if TASK_RELIABILITY_CONTRACTS[task_name].retry_policy in REDRIVE_POLICIES
        and TASK_RELIABILITY_CONTRACTS[task_name].idempotency
        is Idempotency.NON_IDEMPOTENT
        and TASK_RELIABILITY_CONTRACTS[task_name].failure_visibility
        not in OPERATOR_REACHABLE_FAILURES
    }

    assert not unreachable, (
        "These non-idempotent tasks claim an operator redrive but expose no "
        "surface to see or trigger one. Build the redrive path in its owning "
        "service (for tracked device work, network.operation_ledger) and give "
        "the task an operator-reachable failure visibility, or declare the "
        f"policy the code actually implements: {unreachable}"
    )


def test_non_idempotent_tasks_are_never_blindly_autoretried():
    """Blind Celery autoretry of non-idempotent work re-runs the side effect."""
    unsafe = {
        task_name
        for task_name in _first_party_registered_tasks()
        if TASK_RELIABILITY_CONTRACTS[task_name].idempotency
        is Idempotency.NON_IDEMPOTENT
        and TASK_RELIABILITY_CONTRACTS[task_name].retry_policy
        is RetryPolicy.CELERY_AUTORETRY
    }

    assert not unsafe, (
        "Non-idempotent tasks must not rely on blind Celery autoretry; a retry "
        f"repeats the side effect. Guard them or redrive explicitly: {unsafe}"
    )
