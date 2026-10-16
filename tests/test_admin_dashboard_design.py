from pathlib import Path

TEMPLATES = Path("templates/admin/dashboard")


def test_dashboard_keeps_attendance_spaced_from_primary_action() -> None:
    source = (TEMPLATES / "index.html").read_text(encoding="utf-8")

    header = source[source.index("<header class=") : source.index("</header>")]
    assert 'class="flex flex-col gap-3 sm:flex-row' in header
    assert header.index('id="attendance-widget"') < header.index('"Add Customer"')


def test_dashboard_projects_responsive_light_and_dark_layout() -> None:
    source = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    stats = (TEMPLATES / "_stats.html").read_text(encoding="utf-8")

    assert "xl:grid-cols-2" in source
    assert "grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4" in stats
    assert "dark:bg-slate-800" in source
    assert "dark:bg-slate-800" in stats
    assert '"Customer"' in stats
    assert '"%d active, %+d this month"' in stats
    assert '"%d / %d Online Network Devices"' in stats


def test_ticket_performance_is_ticket_only_and_team_filterable() -> None:
    source = (TEMPLATES / "_ticket_performance.html").read_text(encoding="utf-8")

    assert "Tickets Performance" in source
    assert 'id="ticket-team-filter"' in source
    assert "Breached SLA" in source
    assert "Close to SLA" in source
    assert "Newly created" in source
    assert "Within SLA" not in source
    assert "customer" not in source.lower()


def test_activity_follows_both_performance_cards() -> None:
    source = (TEMPLATES / "index.html").read_text(encoding="utf-8")

    trend = source.index("_performance_trend.html")
    tickets = source.index("_ticket_performance.html")
    activity = source.index("_activity.html")
    assert trend < activity
    assert tickets < activity


def test_performance_trend_is_admin_gated_and_non_admin_reflows_activity() -> None:
    source = (TEMPLATES / "index.html").read_text(encoding="utf-8")

    assert "{% if show_performance_trend %}" in source
    non_admin = source[
        source.index("{% else %}", source.index("{% if show_performance_trend %}")) :
    ]
    assert "_performance_trend.html" not in non_admin.split("{% endif %}", 1)[0]
    assert "_ticket_performance.html" in non_admin
    assert "_activity.html" in non_admin


def test_my_work_renders_ticket_mentions() -> None:
    source = (TEMPLATES / "_my_work.html").read_text(encoding="utf-8")

    assert "mention_work_items" in source
    assert "Mentioned in {{ item.ticket_reference }}" in source
    assert "{{ item.href }}" in source


def test_whats_new_is_amber_and_dismissible() -> None:
    source = (TEMPLATES / "index.html").read_text(encoding="utf-8")

    assert "bg-gradient-to-r from-amber-50" in source
    assert "dotmac_whats_new_dismissed" in source
    assert 'aria-label="Dismiss What\'s new"' in source
