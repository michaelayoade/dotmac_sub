from pathlib import Path

from app.services.ip_consistency_audit import (
    _COLLISION_KINDS,
    CRITICAL_KINDS,
    INFORMATIONAL_KINDS,
    WARNING_KINDS,
)

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "deploy" / "observability" / "ip_consistency.rules.yml"


def _blocks() -> dict[str, str]:
    rules = RULES.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for chunk in rules.split("- alert: ")[1:]:
        out[chunk.split("\n")[0].strip()] = chunk
    return out


def test_every_collision_kind_is_classified_exactly_once() -> None:
    tiers = (set(CRITICAL_KINDS), set(WARNING_KINDS), set(INFORMATIONAL_KINDS))
    assert set().union(*tiers) == set(_COLLISION_KINDS)
    assert sum(len(t) for t in tiers) == len(_COLLISION_KINDS)


def test_collision_alert_fires_critical_only_on_critical_kinds() -> None:
    rules = RULES.read_text(encoding="utf-8")
    assert "radius_ip_consistency_drift" in rules
    assert "> 0" in rules
    runbook = "docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md"
    assert f'runbook: "{runbook}"' in rules
    assert (ROOT / runbook).is_file()

    block = _blocks()["IPv4CollisionDetected"]
    expr = block.split("expr:")[1].split("for:")[0]
    assert "severity: critical" in block
    for kind in CRITICAL_KINDS:
        assert kind in expr
    for kind in (*WARNING_KINDS, *INFORMATIONAL_KINDS):
        assert kind not in expr


def test_warning_kinds_warn_and_informational_kinds_never_alert() -> None:
    blocks = _blocks()
    for kind in WARNING_KINDS:
        matching = [b for b in blocks.values() if f'kind="{kind}"' in b]
        assert len(matching) == 1, kind
        assert "severity: warning" in matching[0]
    for name, block in blocks.items():
        expr = block.split("expr:")[1].split("for:")[0]
        for kind in INFORMATIONAL_KINDS:
            assert kind not in expr, (name, kind)


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
