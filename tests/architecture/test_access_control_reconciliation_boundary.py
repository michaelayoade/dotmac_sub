"""The periodic access loop consumes owners and does not redefine policy."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_radius_task_uses_canonical_projection_comparator() -> None:
    source = (ROOT / "app/tasks/radius.py").read_text()

    assert "plan_login_radius_projections" in source
    assert "compare_radius_projection" in source
    assert "RADIUS_BLOCKING_SUBSCRIBER_STATUSES" not in source
    assert "blocked_subscriber_ids" not in source
    assert "expected_wg" not in source


def test_account_projection_has_one_periodic_owner() -> None:
    task_source = (ROOT / "app/tasks/enforcement.py").read_text()
    reliability = (ROOT / "app/services/task_reliability.py").read_text()

    assert "reconcile_account_status_drift" not in task_source
    assert "reconcile_account_status_drift" not in reliability
