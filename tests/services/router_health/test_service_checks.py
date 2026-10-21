from __future__ import annotations

from app.services.router_health import service_checks as sc
from app.services.router_health.contracts import (
    CanaryDisposition,
    CheckId,
    HealthPriority,
)


def test_gwarimpa_like_timeouts_fire() -> None:
    counts = {"timeout": 14, "disconnected": 7, "temporarily_suppressed": 30}
    (finding,) = sc.evaluate_coa_timeouts(
        "Gwarimpa", counts, window_hours=72, min_count=3, ratio_pct=20
    )
    assert finding.check is CheckId.coa_timeouts
    assert finding.priority is HealthPriority.high
    assert finding.evidence["count_temporarily_suppressed"] == 30


def test_gudu_like_all_acks_does_not_fire() -> None:
    assert (
        sc.evaluate_coa_timeouts(
            "Gudu", {"disconnected": 100}, window_hours=72, min_count=3, ratio_pct=20
        )
        == []
    )


def test_below_count_or_ratio_does_not_fire_and_disabled_not_counted() -> None:
    assert not sc.evaluate_coa_timeouts(
        "x", {"timeout": 2}, window_hours=72, min_count=3, ratio_pct=20
    )
    assert not sc.evaluate_coa_timeouts(
        "x",
        {"timeout": 3, "disconnected": 97},
        window_hours=72,
        min_count=3,
        ratio_pct=20,
    )
    assert sc.evaluate_coa_timeouts(
        "x", {"timeout": 3, "disabled": 500}, window_hours=72, min_count=3, ratio_pct=20
    )


def test_enforcement_paths_group_and_priority() -> None:
    rows = [
        sc.EnforcementFailureRow("api", "auth_rejected", 5),
        sc.EnforcementFailureRow("ssh", "unreachable", 5),
        sc.EnforcementFailureRow("ssh", "timeout", 2),
    ]
    found = sc.evaluate_enforcement_paths("n", rows, min_count=3)
    assert {f.qualifier for f in found} == {"api-auth-rejected", "ssh-unreachable"}
    assert {f.priority for f in found} == {HealthPriority.high, HealthPriority.medium}


def test_canary_outcome_mapping() -> None:
    for healthy in (CanaryDisposition.rejected, CanaryDisposition.session_not_found):
        assert sc.evaluate_canary_result("n", healthy, reprobe_failed=False) == []
    (ack,) = sc.evaluate_canary_result(
        "n", CanaryDisposition.disconnected, reprobe_failed=False
    )
    assert (
        ack.priority is HealthPriority.critical
        and ack.check is CheckId.coa_canary_acked
    )
    assert (
        sc.evaluate_canary_result("n", CanaryDisposition.timeout, reprobe_failed=False)
        == []
    )
    (timeout,) = sc.evaluate_canary_result(
        "n", CanaryDisposition.timeout, reprobe_failed=True
    )
    assert timeout.priority is HealthPriority.high
    (config,) = sc.evaluate_canary_result(
        "n", CanaryDisposition.missing_secret, reprobe_failed=False
    )
    assert config.check is CheckId.coa_canary_config
