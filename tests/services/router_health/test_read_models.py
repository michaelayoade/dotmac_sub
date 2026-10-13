"""Read models and the router list / detail / drift rendering."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.router_management import Router, RouterStatus
from app.services import web_network_routers
from app.services.router_health import findings, read_models
from app.services.router_health.contracts import (
    CheckId,
    HealthFinding,
    HealthLayer,
    HealthPriority,
    SubjectKind,
)
from app.web.admin.network_routers import templates

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def add_router(db, name, status=RouterStatus.online):
    router = Router(
        name=name,
        hostname=name,
        management_ip="192.0.2.20",
        rest_api_username="u",
        rest_api_password="p",
        status=status,
        is_active=True,
    )
    db.add(router)
    db.commit()
    return router


def finding(router_id, check, priority, **evidence):
    return HealthFinding(
        layer=HealthLayer.runtime,
        subject_kind=SubjectKind.router,
        subject_id=router_id,
        check=check,
        qualifier="0",
        priority=priority,
        title=f"Router {check.value}",
        impact="Something stopped working.",
        evidence={"peer": "198.51.100.9", **evidence},
        fix="Fix the thing.",
        target_url=f"/admin/network/routers/{router_id}#health",
        router_id=router_id,
        known_issue="documented upgrade pending",
    )


def record(db, *items, mode="shadow"):
    findings.sync_findings(db, findings=list(items), scopes=[], mode=mode)
    db.commit()


def test_badge_is_worst_open_priority_and_ignores_router_status(db_session) -> None:
    sick = add_router(db_session, "rm-sick", status=RouterStatus.online)
    well = add_router(db_session, "rm-well", status=RouterStatus.unreachable)
    empty = add_router(db_session, "rm-empty")
    record(
        db_session,
        finding(sick.id, CheckId.rest_unreachable, HealthPriority.high),
        finding(sick.id, CheckId.clock_skew, HealthPriority.medium),
        finding(sick.id, CheckId.router_rebooted, HealthPriority.low),
    )
    from app.models.router_health import RouterHealthSignal

    db_session.add(
        RouterHealthSignal(
            router_id=well.id,
            signal="rest_access",
            scope="router",
            status="ok",
            samples=[],
            consecutive_abnormal=0,
            last_observed_at=NOW,
        )
    )
    db_session.commit()
    badges = read_models.router_health_badges(db_session, [sick.id, well.id, empty.id])
    assert badges[sick.id].level == "high"
    assert badges[sick.id].open_count == 3
    assert badges[well.id].level == "ok"  # status "unreachable" is irrelevant
    assert badges[empty.id].level == "no_data"


def test_resolved_findings_do_not_count(db_session) -> None:
    router = add_router(db_session, "rm-a")
    item = finding(router.id, CheckId.rest_unreachable, HealthPriority.high)
    record(db_session, item)
    findings.sync_findings(
        db_session,
        findings=[],
        scopes=[
            findings.Scope(HealthLayer.runtime, router.id, CheckId.rest_unreachable)
        ],
        mode="shadow",
    )
    db_session.commit()
    assert read_models.router_health_findings(db_session, router.id) == ()


def test_finding_views_carry_title_impact_evidence_fix_and_target(db_session) -> None:
    router = add_router(db_session, "rm-a")
    record(
        db_session,
        finding(
            router.id, CheckId.bgp_session_down, HealthPriority.high, down_minutes=95
        ),
    )
    (view,) = read_models.router_health_findings(db_session, router.id)
    assert view.title and view.impact == "Something stopped working."
    assert dict(view.evidence)["down minutes"] == "95"
    assert view.fix == "Fix the thing."
    assert view.known_issue == "documented upgrade pending"
    assert view.target_url.endswith("#health")
    assert view.mode == "shadow"
    assert view.router_name == "rm-a"


def test_detail_reports_modes_and_times(db_session) -> None:
    router = add_router(db_session, "rm-a")
    detail = read_models.router_health_detail(db_session, router.id)
    assert detail.badge.level == "no_data"
    assert detail.alert_mode == "shadow"
    assert detail.last_snapshot_at is None and detail.last_runtime_poll_at is None


def test_posture_summary_hides_detail_without_router_read(db_session) -> None:
    router = add_router(db_session, "rm-a")
    record(
        db_session, finding(router.id, CheckId.rest_auth_failed, HealthPriority.high)
    )
    counts_only = read_models.router_posture_summary(db_session, include_detail=False)
    assert counts_only.total == 1 and counts_only.counts["high"] == 1
    assert counts_only.top == () and not counts_only.detail_visible
    full = read_models.router_posture_summary(db_session, include_detail=True)
    assert len(full.top) == 1


def test_posture_summary_orders_by_priority_and_limits(db_session) -> None:
    router = add_router(db_session, "rm-a")
    record(
        db_session,
        finding(router.id, CheckId.router_rebooted, HealthPriority.low),
        finding(router.id, CheckId.clock_skew, HealthPriority.medium),
        finding(router.id, CheckId.rest_auth_failed, HealthPriority.high),
    )
    summary = read_models.router_posture_summary(
        db_session, include_detail=True, limit=2
    )
    assert [v.priority for v in summary.top] == ["high", "medium"]


@pytest.mark.parametrize("value", ["", None, "bogus"])
def test_unknown_health_filter_is_ignored(value) -> None:
    assert read_models.filter_router_ids_by_health({}, value) is None


# ------------------------------------------------------------------ rendering


class _State:
    csrf_token = "t"
    auth: dict = {"permission_keys": {"*"}}


class _URL:
    path = "/admin/network/routers"

    def __str__(self) -> str:
        return self.path


class DummyRequest:
    state = _State()
    query_params: dict = {}
    headers: dict = {}
    cookies: dict = {}
    url = _URL()
    session: dict = {}
    client = None
    scope: dict = {}

    def url_for(self, *args, **kwargs) -> str:
        return "/"


def render(name, context) -> str:
    return templates.env.get_template(name).render(**context)


def test_router_list_renders_health_column_badge_filter_and_shadow_pill(
    db_session,
) -> None:
    sick = add_router(db_session, "rm-sick")
    add_router(db_session, "rm-new")
    record(db_session, finding(sick.id, CheckId.rest_auth_failed, HealthPriority.high))
    context = web_network_routers.list_context(DummyRequest(), db_session)
    html = render("admin/network/routers/index.html", context)
    assert ">Health" in html
    assert 'data-health-badge="high"' in html
    assert 'data-health-badge="no_data"' in html
    assert "Shadow" in html
    assert 'name="health"' in html


def test_router_list_health_filter_narrows_rows(db_session) -> None:
    sick = add_router(db_session, "rm-sick")
    add_router(db_session, "rm-new")
    record(db_session, finding(sick.id, CheckId.rest_auth_failed, HealthPriority.high))
    context = web_network_routers.list_context(
        DummyRequest(), db_session, health="high"
    )
    assert [r.name for r in context["routers"]] == ["rm-sick"]
    assert context["pagination"]["total"] == 1
    html = render("admin/network/routers/index.html", context)
    assert "rm-new" not in html


def test_router_detail_renders_each_finding_part(db_session) -> None:
    router = add_router(db_session, "rm-a")
    record(
        db_session,
        finding(
            router.id, CheckId.bgp_session_down, HealthPriority.high, down_minutes=95
        ),
    )
    context = web_network_routers.detail_context(
        DummyRequest(), db_session, router_id=router.id
    )
    html = render("admin/network/routers/detail.html", context)
    assert 'id="health"' in html
    for expected in (
        "Router bgp-session-down",
        "Something stopped working.",
        "down minutes",
        "95",
        "Fix the thing.",
        "documented upgrade pending",
        f"/admin/network/routers/{router.id}#health",
        "Last configuration snapshot",
        "Last runtime poll",
        "Shadow mode",
    ):
        assert expected in html


def test_router_detail_without_findings_says_so(db_session) -> None:
    router = add_router(db_session, "rm-a")
    context = web_network_routers.detail_context(
        DummyRequest(), db_session, router_id=router.id
    )
    html = render("admin/network/routers/detail.html", context)
    assert "No health data has been collected" in html


def test_drift_page_router_posture_respects_detail_permission(db_session) -> None:
    router = add_router(db_session, "rm-a")
    record(
        db_session, finding(router.id, CheckId.rest_auth_failed, HealthPriority.high)
    )
    base = {
        "request": DummyRequest(),
        "active_page": "admin-drift",
        "active_menu": "dashboard",
        "current_user": {"name": "T", "email": "t@example.com"},
        "sidebar_stats": {},
        "findings": [],
        "open_by_severity": {},
        "breached_count": 0,
        "statuses": [],
        "severities": [],
        "checks": [],
        "entity_types": [],
        "total": 0,
        "total_pages": 1,
        "page": 1,
        "per_page": 50,
        "status": "open",
        "severity": None,
        "check": None,
        "entity_type": None,
        "owner": "",
    }
    allowed = render(
        "admin/drift/index.html",
        {
            **base,
            "router_posture": read_models.router_posture_summary(
                db_session, include_detail=True
            ),
        },
    )
    assert "Router posture" in allowed
    assert "Fix the thing." in allowed and "198.51.100.9" in allowed
    counts = render(
        "admin/drift/index.html",
        {
            **base,
            "router_posture": read_models.router_posture_summary(
                db_session, include_detail=False
            ),
        },
    )
    assert "Router posture" in counts
    assert "198.51.100.9" not in counts and "Fix the thing." not in counts
    assert "need the router:read permission" in counts
