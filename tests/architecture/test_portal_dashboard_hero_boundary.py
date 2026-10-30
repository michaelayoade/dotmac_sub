"""Keep the customer dashboard hero decision in its registered read owner."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_dashboard_template_renders_typed_hero_without_redeciding_priority() -> None:
    template = (ROOT / "templates/customer/dashboard/index.html").read_text(
        encoding="utf-8"
    )
    hero = template.split("{% set hero = dashboard_highlights.hero %}", 1)[1].split(
        "{% if dashboard_highlights.services|length", 1
    )[0]
    assert "hero.message" in hero
    assert "account_health.primary_action" not in template
    assert "area_outage" not in hero
    assert "prepaid_funding" not in hero
    assert "renewal_shortfall" not in hero


def test_dashboard_hero_has_one_registered_owner() -> None:
    declaration = (
        ROOT / "app/services/sot_registry/domains/ui_semantic_presentation.py"
    ).read_text(encoding="utf-8")
    relationship_map = (ROOT / "docs/SOT_RELATIONSHIP_MAP.md").read_text(
        encoding="utf-8"
    )
    concern = "customer dashboard renewal warning and outage hero"
    assert declaration.count(f'name="{concern}"') == 1
    assert relationship_map.count(f"| {concern} |") == 1
