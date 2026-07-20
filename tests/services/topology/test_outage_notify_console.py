"""Operator outage-notification console — route/permission wiring + audit read.

The dispatch *behaviour* (flag-off no-op, actor requirement, persisted debounce,
audit rows) is covered in test_outage_notifications.py; the route only previews
and, on an explicit confirm POST, delegates to that hard-gated service. These
tests pin the wiring that makes the route safe: it exists, the confirm POST is
behind ``monitoring:write`` (preview behind ``monitoring:read``), the template
renders, and the audit read returns what the console shows.
"""

from __future__ import annotations

import uuid

from fastapi.templating import Jinja2Templates


def _find(method: str, path: str):
    from app.web.admin.network_monitoring import router

    for r in router.routes:
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set()):
            return r
    return None


def _perm_key_of(route) -> str | None:
    """The require_permission key gating a route (from the closure), if any."""
    for dep in route.dependant.dependencies:
        call = getattr(dep, "call", None)
        if call is None or getattr(call, "__name__", "") != "_require_permission":
            continue
        for cell in call.__closure__ or ():
            val = cell.cell_contents
            if isinstance(val, str) and ":" in val:
                return val
    return None


def test_notify_routes_registered():
    assert _find("GET", "/network/detected-outages/notify") is not None
    assert _find("POST", "/network/detected-outages/notify") is not None


def test_notify_preview_requires_monitoring_read():
    assert _perm_key_of(_find("GET", "/network/detected-outages/notify")) == (
        "monitoring:read"
    )


def test_notify_confirm_requires_monitoring_write():
    # The only dispatch path must be gated on write, not read.
    assert _perm_key_of(_find("POST", "/network/detected-outages/notify")) == (
        "monitoring:write"
    )


def test_notify_template_compiles():
    Jinja2Templates(directory="templates").env.get_template(
        "admin/network/detected_outages_notify.html"
    )


def test_recent_dispatches_returns_rows_and_counts(db_session):
    from app.models.network_monitoring import OutageNotificationDispatch
    from app.services.topology.outage_notifications import recent_dispatches

    boundary = uuid.uuid4()
    db_session.add_all(
        [
            OutageNotificationDispatch(
                scope="area",
                boundary_node_id=boundary,
                channel="outage_area",
                category="service",
                recipient="a@example.io",
                subject="Service interruption in your area",
                dedup_key=f"area:{boundary}:1",
                status="sent",
                actor_id=uuid.uuid4(),
            ),
            OutageNotificationDispatch(
                scope="area",
                boundary_node_id=boundary,
                channel="outage_area",
                category="service",
                dedup_key=f"area:{boundary}:2",
                status="suppressed_optout",
            ),
        ]
    )
    db_session.commit()

    out = recent_dispatches(db_session, boundary)
    assert out["counts"].get("sent") == 1
    assert out["counts"].get("suppressed_optout") == 1
    assert len(out["rows"]) == 2
    assert out["rows"][0]["recipient"] in {"a@example.io", None}


def test_recent_dispatches_empty_for_unknown_boundary(db_session):
    from app.services.topology.outage_notifications import recent_dispatches

    out = recent_dispatches(db_session, uuid.uuid4())
    assert out == {"rows": [], "counts": {}}
