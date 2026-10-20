from pathlib import Path

from app.services.ip_consistency_audit import _COLLISION_KINDS

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "deploy" / "observability" / "ip_consistency.rules.yml"


def test_collision_alert_covers_every_hard_collision_kind() -> None:
    rules = RULES.read_text(encoding="utf-8")

    assert "IPv4CollisionDetected" in rules
    assert "radius_ip_consistency_drift" in rules
    for kind in _COLLISION_KINDS:
        assert kind in rules
    assert "> 0" in rules
    runbook = "docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md"
    assert f'runbook: "{runbook}"' in rules
    assert (ROOT / runbook).is_file()


def test_terminal_assignment_kind_only_warns() -> None:
    rules = RULES.read_text(encoding="utf-8")
    critical = rules.split("IPv4AssignmentOnTerminalSubscription")[0]
    assert "assignment_on_terminal_subscription" not in critical.split("expr:")[1]
    block = rules.split("alert: IPv4AssignmentOnTerminalSubscription")[1].split(
        "- alert:"
    )[0]
    assert "severity: warning" in block
    assert 'kind="assignment_on_terminal_subscription"' in block


def test_stale_audit_threshold_is_at_least_twice_the_default_interval() -> None:
    import re

    rules = RULES.read_text(encoding="utf-8")
    match = re.search(r"radius_ip_consistency_audit_age_seconds > (\d+)", rules)
    assert match is not None
    from app.services.settings_spec import SETTINGS_SPECS

    default = next(
        spec.default
        for spec in SETTINGS_SPECS
        if spec.key == "ip_consistency_audit_interval_seconds"
    )
    assert int(match.group(1)) >= 2 * int(default)
