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
    assert "service.uptime_lower_seconds" in admin_cards
    assert "service.confirmed_downtime_seconds" in admin_cards
    assert admin_cards.count('aria-label="How uptime was calculated"') == 1
    assert admin_cards.count('aria-label="How downtime was calculated"') == 1
    assert "availability_stats.service" in admin_cards
    assert "availability_stats.services" not in admin_cards
    assert "availability_history_query" not in admin_cards
    for label in ("Total data used", "Download", "Upload", "Daily average"):
        card = admin_cards.split(f">{label}</p>", 1)[1].split("</div>", 1)[0]
        assert "<details" not in card
    route = (ROOT / "app/web/admin/customers.py").read_text()
    stats_route = route.split("def person_detail_stats(", 1)[1].split("@router.get", 1)[
        0
    ]
    assert "query_customer_statistics_availability(" in stats_route
    assert "query_customer_availability(" not in stats_route
    assert "AvailabilitySnapshot" not in stats_route


def test_statistics_owner_uses_only_authoritative_period_evidence():
    source = (ROOT / "app/services/customer_service_level.py").read_text()
    query = source.split("def _statistics_service_periods(", 1)[1].split(
        "def _complete_verdict", 1
    )[0]
    assert "prepaid_coverage_history_for_period(" in query
    assert "postpaid_entitlement_history_for_period(" in query
    assert "lifecycle_history_for_period(" in query
    assert "_availability_evidence(" in query
    assert "SubscriptionStatus.active" in query
    assert "SubscriptionStatus.paused" in query
    assert "SubscriptionStatus.suspended" in query
    for excluded in (
        "pending",
        "blocked",
        "stopped",
        "disabled",
        "expired",
        "canceled",
        "hidden",
        "archived",
    ):
        assert f"SubscriptionStatus.{excluded}" not in query
    assert "selected, period = max(" in query
    assert "get_current_path" not in query
    assert "Ticket" not in query
    assert "AvailabilitySnapshot" not in query
