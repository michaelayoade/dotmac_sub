"""The operator planner is read-only and cannot mutate runtime activation."""

from pathlib import Path


def test_planner_has_no_activation_or_readiness_writer_options() -> None:
    script = Path("scripts/one_off/plan_prepaid_balance_sweep.py").read_text()

    assert "--activation-at" not in script
    assert "--record-readiness" not in script
    assert "activated_at" not in script
    assert ".commit(" not in script
    assert "run_prepaid_balance_sweep" not in script
