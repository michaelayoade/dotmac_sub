"""Customer detail page: online chip, ONT live line, hidden Active badges.

Presentation only; renders the shared record.html macros and checks the
detail template wiring.
"""

from __future__ import annotations

from pathlib import Path


# ---------------------------------------------------------------------------
# Online status chip, ONT live line, hidden "Active" badge (presentation only)
# ---------------------------------------------------------------------------


def _record_env():
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    env.filters["app_datetime"] = lambda value, fmt="": value.strftime(fmt)
    return env


def _render_macro(source: str, **context) -> str:
    return (
        _record_env()
        .from_string(
            '{% from "components/ui/record.html" import customer_online_chip, '
            "ont_live_status, subscriber_hero %}" + source
        )
        .render(**context)
    )


def test_online_chip_connected_shows_session_count():
    html = _render_macro(
        "{{ customer_online_chip(s, {}) }}",
        s={"state": "connected", "connected_count": 2},
    )
    assert "Online · 2 sessions" in html


def test_online_chip_offline_shows_newest_last_seen_and_stale():
    from datetime import UTC, datetime

    by_sub = {
        "a": {"last_seen_at": datetime(2026, 11, 1, 8, 0, tzinfo=UTC)},
        "b": {"last_seen_at": datetime(2026, 11, 3, 9, 30, tzinfo=UTC)},
        "c": {"last_seen_at": None},
    }
    html = _render_macro(
        "{{ customer_online_chip(s, b) }}", s={"state": "offline"}, b=by_sub
    )
    assert "Offline · last seen Nov 03, 09:30" in html
    bare = _render_macro("{{ customer_online_chip(s, {}) }}", s={"state": "offline"})
    assert "Offline" in bare and "last seen" not in bare
    stale = _render_macro("{{ customer_online_chip(s, {}) }}", s={"state": "stale"})
    assert "Stale" in stale


def test_online_chip_renders_nothing_without_snapshot():
    assert _render_macro("{{ customer_online_chip({}, {}) }}").strip() == ""
    assert _render_macro("{{ customer_online_chip(None) }}").strip() == ""


def test_ont_live_status_renders_only_present_values():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    ont = SimpleNamespace(
        olt_status=SimpleNamespace(value="offline"),
        last_seen_at=datetime(2026, 11, 2, 7, 5, tzinfo=UTC),
        onu_rx_signal_dbm=-27.04,
    )
    html = _render_macro("{{ ont_live_status(o) }}", o=ont)
    assert "ONT offline" in html
    assert "Last seen Nov 02, 07:05" in html
    assert "Rx -27.0 dBm" in html
    empty = SimpleNamespace(olt_status=None, last_seen_at=None, onu_rx_signal_dbm=None)
    html = _render_macro("{{ ont_live_status(o) }}", o=empty)
    assert "Rx" not in html and "Last seen" not in html and "ONT " not in html


def _hero(status_value: str, hide: bool) -> str:
    from types import SimpleNamespace

    summary = SimpleNamespace(
        name="Test Customer",
        avatar_url=None,
        is_business=False,
        subscriber_number=None,
        since=None,
        email=None,
        phone=None,
        plan=None,
        balance=None,
        connection=None,
        status=None,
    )
    presentation = SimpleNamespace(
        value=status_value, label=status_value.title() + "BADGE", tone="neutral"
    )
    return _render_macro(
        "{{ subscriber_hero(summary, p, hide_active_status=hide) }}",
        summary=summary,
        p=presentation,
        hide=hide,
    )


def test_hero_hides_active_badge_but_keeps_other_statuses():
    assert (
        "ActiveBADGE" not in _hero("active", True).split("</h1>")[1].split("</div>")[0]
    )
    assert (
        "SuspendedBADGE"
        in _hero("suspended", True).split("</h1>")[1].split("</div>")[0]
    )
    # default behaviour unchanged for other pages
    assert "ActiveBADGE" in _hero("active", False).split("</h1>")[1].split("</div>")[0]


def test_detail_template_hides_active_badges_and_labels_session():
    template = Path("templates/admin/customers/detail.html").read_text(encoding="utf-8")
    assert "pulse=(subscriber_status_presentation.value" not in template
    assert "pulse=(sub_status == 'active')" not in template
    assert "card.status_presentation.value != 'active'" in template
    assert "Session:" in template
    assert "'Last seen '" in template
    assert "customer_online_chip(network_connection_status" in template
    assert template.count("ont_live_status(assignment.ont_unit)") == 2
