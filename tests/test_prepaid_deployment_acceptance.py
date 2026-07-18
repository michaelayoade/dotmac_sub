from scripts.one_off.verify_prepaid_deployment_acceptance import (
    AcceptanceExpectation,
    AcceptanceObservation,
    evaluate_acceptance,
)


def _expectation() -> AcceptanceExpectation:
    return AcceptanceExpectation(
        git_sha="a" * 40,
        alembic_head="329_prepaid_service_renewal",
        minimum_active_baselines=4265,
        plan_sha256="b" * 64,
        plan_entry_count=3,
        plan_total_amount="112875.00",
        plan_already_reconciled=0,
        renewal_control_enabled=False,
    )


def _observation() -> AcceptanceObservation:
    return AcceptanceObservation(
        git_sha="a" * 40,
        alembic_heads=("329_prepaid_service_renewal",),
        active_baselines=4265,
        authority_cutover_batches=1,
        active_readiness_records=1,
        enforcement_enabled=True,
        renewal_control_enabled=False,
        activation_at="2026-07-17T06:30:00+00:00",
        activation_error=None,
        readiness_block_reason=None,
        plan_sha256="b" * 64,
        plan_entries=3,
        plan_total_amount="112875.00",
        plan_accounts=3,
        plan_blocked_accounts=0,
        plan_already_reconciled=0,
        plan_ready=True,
    )


def test_acceptance_requires_every_deployment_and_financial_gate():
    report = evaluate_acceptance(_observation(), _expectation())

    assert report["ready"] is True
    assert all(report["checks"].values())


def test_acceptance_fails_closed_when_plan_was_applied_or_readiness_blocked():
    current = _observation()
    observation = AcceptanceObservation(
        **{
            **current.__dict__,
            "readiness_block_reason": "prepaid_funding_readiness_cohort_changed",
            "plan_already_reconciled": 3,
        }
    )

    report = evaluate_acceptance(observation, _expectation())

    assert report["ready"] is False
    assert report["checks"]["readiness_unblocked"] is False
    assert report["checks"]["plan_reconciliation_state"] is False


def test_acceptance_fails_closed_on_wrong_revision_or_plan():
    current = _observation()
    observation = AcceptanceObservation(
        **{
            **current.__dict__,
            "git_sha": "c" * 40,
            "plan_sha256": "d" * 64,
            "plan_total_amount": "1.00",
        }
    )

    report = evaluate_acceptance(observation, _expectation())

    assert report["ready"] is False
    assert report["checks"]["git_sha"] is False
    assert report["checks"]["plan_sha256"] is False
    assert report["checks"]["plan_total_amount"] is False
