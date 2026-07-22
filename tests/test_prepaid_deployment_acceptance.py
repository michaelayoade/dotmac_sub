from scripts.one_off.verify_prepaid_deployment_acceptance import (
    AcceptanceExpectation,
    AcceptanceObservation,
    evaluate_acceptance,
)


def _expectation() -> AcceptanceExpectation:
    return AcceptanceExpectation(
        git_sha="a" * 40,
        alembic_head="394_retire_payment_prepaid_applications",
        minimum_active_baselines=4265,
        coverage_fingerprint="b" * 64,
        coverage_subscription_count=2925,
    )


def _observation() -> AcceptanceObservation:
    return AcceptanceObservation(
        git_sha="a" * 40,
        alembic_heads=("394_retire_payment_prepaid_applications",),
        active_baselines=4265,
        authority_cutover_batches=1,
        coverage_fingerprint="b" * 64,
        coverage_subscription_count=2925,
        coverage_repairable_count=0,
        coverage_quarantined_count=0,
        coverage_blocker_count=0,
    )


def test_acceptance_requires_every_deployment_and_coverage_gate():
    report = evaluate_acceptance(_observation(), _expectation())

    assert report["ready"] is True
    assert all(report["checks"].values())


def test_acceptance_fails_closed_when_coverage_needs_repair_or_quarantine():
    current = _observation()
    observation = AcceptanceObservation(
        **{
            **current.__dict__,
            "coverage_repairable_count": 3,
            "coverage_quarantined_count": 2,
            "coverage_blocker_count": 5,
        }
    )

    report = evaluate_acceptance(observation, _expectation())

    assert report["ready"] is False
    assert report["checks"]["coverage_repair_complete"] is False
    assert report["checks"]["coverage_quarantine_empty"] is False
    assert report["checks"]["coverage_unblocked"] is False


def test_acceptance_fails_closed_on_wrong_revision_or_coverage_fingerprint():
    current = _observation()
    observation = AcceptanceObservation(
        **{
            **current.__dict__,
            "git_sha": "c" * 40,
            "coverage_fingerprint": "d" * 64,
            "coverage_subscription_count": 1,
        }
    )

    report = evaluate_acceptance(observation, _expectation())

    assert report["ready"] is False
    assert report["checks"]["git_sha"] is False
    assert report["checks"]["coverage_fingerprint"] is False
    assert report["checks"]["coverage_subscription_count"] is False
