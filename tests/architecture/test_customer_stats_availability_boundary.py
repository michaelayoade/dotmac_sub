"""Guard the admin Statistics availability boundary."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_availability_owner_keeps_session_gaps_out_of_provider_downtime():
    source = (ROOT / "app/services/topology/customer_availability.py").read_text()
    assert "RadiusAccountingSession" not in source
    assert "RadiusActiveSession" not in source
    assert (
        "row.downtime_seconds"
        not in source.split("def query_customer_availability", 1)[1]
    )


def test_stats_cards_remain_admin_only_and_route_is_an_adapter():
    template = (ROOT / "templates/customer/usage/_content.html").read_text()
    assert "{% if usage_theme == 'admin' %}" in template
    admin_cards = template.split("{% if usage_theme == 'admin' %}", 1)[1]
    assert "availability_stats.uptime_days" in admin_cards
    assert "availability_stats.downtime_days" in admin_cards
    route = (ROOT / "app/web/admin/customers.py").read_text()
    stats_route = route.split("def person_detail_stats(", 1)[1].split("@router.get", 1)[
        0
    ]
    assert "query_customer_availability(" in stats_route
    assert "AvailabilitySnapshot" not in stats_route
