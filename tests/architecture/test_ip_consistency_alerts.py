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
