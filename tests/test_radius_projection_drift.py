"""RADIUS writer and auditor share one desired-state comparator."""

from types import SimpleNamespace
from typing import Any, cast

from app.services.radius_projection_planner import compare_radius_projection


def _desired(**modes: str) -> dict[str, Any]:
    return {
        login: cast(Any, SimpleNamespace(plan=SimpleNamespace(mode=mode)))
        for login, mode in modes.items()
    }


def test_projection_drift_is_bidirectional() -> None:
    drift = compare_radius_projection(
        _desired(active="active", blocked="reject", portal="captive"),
        observed_auth={"active", "blocked", "stale"},
        observed_reject={"stale"},
        observed_captive={"stale"},
    )

    assert drift.missing_auth == {"portal"}
    assert drift.missing_reject == {"blocked"}
    assert drift.stale_reject == {"stale"}
    assert drift.missing_captive == {"portal"}
    assert drift.stale_captive == {"stale"}
    assert drift.usernames == {"blocked", "portal", "stale"}


def test_projection_drift_is_empty_at_parity() -> None:
    drift = compare_radius_projection(
        _desired(active="active", blocked="reject", portal="captive"),
        observed_auth={"active", "blocked", "portal"},
        observed_reject={"blocked"},
        observed_captive={"portal"},
    )

    assert not drift.usernames
