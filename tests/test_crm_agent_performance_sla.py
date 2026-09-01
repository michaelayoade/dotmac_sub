from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import crm_reporting
from app.web.admin import reports as report_routes


def _support_request(user_id=None):
    return SimpleNamespace(
        state=SimpleNamespace(
            auth={"permission_keys": {"reports:support:read"}},
            user=SimpleNamespace(id=user_id or uuid4()),
        )
    )


def _analytics_page(*, person_id=None, team_id=None, sla=False):
    person_id = person_id or uuid4()
    team_id = team_id or uuid4()
    return crm_reporting.team_inbox_metrics.InboxAgentPerformanceAnalyticsPage(
        rows=(
            crm_reporting.team_inbox_metrics.InboxAgentPerformanceAnalyticsRow(
                person_id=person_id,
                agent_name="Ada Agent",
                service_team_id=team_id,
                service_team_name="Support",
                assigned_conversation_count=8,
                resolved_conversation_count=6,
                active_assignment_count=2,
                average_resolution_seconds=1800,
                average_first_response_seconds=300,
                resolution_observation_count=6,
                first_response_observation_count=8,
                first_response_sla_seconds=600 if sla else None,
                first_response_sla_met_count=7 if sla else 0,
                first_response_sla_breached_count=1 if sla else 0,
                first_response_sla_rate=0.875 if sla else None,
            ),
        ),
        summary=crm_reporting.team_inbox_metrics.InboxAgentPerformanceSummary(
            agent_count=1,
            activity_agent_count=1,
            assigned_conversation_count=8,
            resolved_conversation_count=6,
            active_assignment_count=2,
            average_resolution_seconds=1800,
            average_first_response_seconds=300,
            first_response_observation_count=8,
            resolution_observation_count=6,
            first_response_sla_met_count=7 if sla else 0,
            first_response_sla_breached_count=1 if sla else 0,
            first_response_sla_rate=0.875 if sla else None,
            first_response_sla_configured=sla,
        ),
        total=1,
        page=1,
        per_page=50,
        generated_at=datetime.now(UTC),
    )


def test_duration_label_uses_ascii_placeholder_for_missing_values():
    assert crm_reporting._duration_label(None) == "-"
    assert crm_reporting._duration_label(None).isascii()


def test_agent_performance_period_uses_lagos_calendar_boundaries():
    period = crm_reporting.resolve_agent_performance_period(
        preset=crm_reporting.AgentPerformancePeriodPreset.MONTH,
        now=datetime(2026, 8, 28, 23, 30, tzinfo=UTC),
    )

    assert (period.start_date, period.end_date) == (
        date(2026, 8, 1),
        date(2026, 8, 31),
    )
    assert period.start_at == datetime(2026, 7, 31, 23, 0, tzinfo=UTC)
    assert period.end_at == datetime(2026, 8, 31, 23, 0, tzinfo=UTC)


def test_reports_hub_lists_agent_reports_but_not_retired_crm_performance():
    links = [
        link
        for section in report_routes.REPORT_HUB_SECTIONS
        for link in section["links"]
    ]
    names = {link["name"] for link in links}

    assert "Agent Performance" in names
    assert "My Performance" in names
    assert "CRM Performance" not in names


def test_retired_crm_performance_routes_never_execute_builder(monkeypatch):
    monkeypatch.setattr(
        crm_reporting,
        "_crm_performance",
        lambda *_args, **_kwargs: pytest.fail("retired builder executed"),
    )

    page = report_routes.reports_operational_crm_performance_retired()
    export = report_routes.reports_operational_crm_performance_export_retired()

    assert page.status_code == 303
    assert page.headers["location"] == "/admin/reports/inbox-performance"
    assert export.status_code == 303
    assert export.headers["location"] == "/admin/reports/inbox-performance"
    with pytest.raises(crm_reporting.CrmReportQueryError):
        crm_reporting.get_report(
            None,
            slug=crm_reporting.CrmReportSlug.CRM_PERFORMANCE,
            query=crm_reporting.CrmReportQuery(),
        )


@pytest.mark.parametrize("report_slug", ["agent-performance", "my-performance"])
def test_agent_performance_page_returns_lazy_shell(
    db_session, monkeypatch, report_slug
):
    import app.web.admin as admin_web

    monkeypatch.setattr(admin_web, "get_current_user", lambda _request: None)
    monkeypatch.setattr(admin_web, "get_sidebar_stats", lambda _db: {})
    monkeypatch.setattr(
        crm_reporting,
        "get_report",
        lambda *_args, **_kwargs: pytest.fail("lazy shell ran analytics"),
    )
    monkeypatch.setattr(
        report_routes.templates,
        "TemplateResponse",
        lambda _template, context: context,
    )

    context = report_routes.reports_operational_page(
        request=_support_request(),
        report_slug=report_slug,
        range_value="custom",
        date_from="2026-08-01",
        date_to="2026-08-28",
        search="Ada",
        service_team_id=None,
        page=1,
        per_page=50,
        db=db_session,
    )

    assert context["report"] is None
    assert context["lazy_load"] is True
    assert f"/{report_slug}/data?" in context["lazy_data_url"]
    assert "date_from=2026-08-01" in context["lazy_data_url"]
    if report_slug == "my-performance":
        assert "search=" not in context["lazy_data_url"]


def test_data_endpoint_preserves_team_search_and_pagination(db_session, monkeypatch):
    team_id = uuid4()
    captured = []

    def get_report(_db, *, slug, query):
        captured.append((slug, query))
        return crm_reporting.CrmReportPage(
            definition=crm_reporting.REPORT_DEFINITIONS[slug],
            metrics=(),
            columns=("Agent",),
            rows=(),
            total=30,
            page=query.page,
            per_page=query.per_page or 1,
        )

    monkeypatch.setattr(crm_reporting, "get_report", get_report)
    monkeypatch.setattr(
        report_routes.templates,
        "TemplateResponse",
        lambda _template, context, **kwargs: {**context, **kwargs},
    )

    context = report_routes.reports_operational_agent_data(
        request=_support_request(),
        report_slug="agent-performance",
        range_value="custom",
        date_from="2026-08-01",
        date_to="2026-08-28",
        search="Ada Agent",
        service_team_id=str(team_id),
        page=2,
        per_page=25,
        db=db_session,
    )

    _, query = captured[0]
    assert query.service_team_id == team_id
    assert query.search == "Ada Agent"
    assert "page=1" in context["previous_data_url"]
    assert f"service_team_id={team_id}" in context["previous_data_url"]
    assert "search=Ada+Agent" in context["previous_data_url"]


def test_my_performance_query_is_scoped_to_signed_in_user_only():
    user_id = uuid4()
    query = report_routes._operational_report_query(
        request=_support_request(user_id),
        date_from="2026-08-01",
        date_to="2026-08-31",
        page=1,
        per_page=50,
        personal=True,
    )

    assert query.person_id == user_id


@pytest.mark.parametrize(
    ("slug", "person_id"),
    (
        (crm_reporting.CrmReportSlug.AGENT_PERFORMANCE, None),
        (crm_reporting.CrmReportSlug.MY_PERFORMANCE, uuid4()),
    ),
)
def test_agent_reports_without_sla_keep_metrics_and_mark_not_configured(
    db_session, monkeypatch, slug, person_id
):
    analytics = _analytics_page(person_id=person_id, sla=False)
    monkeypatch.setattr(
        crm_reporting.team_inbox_metrics,
        "agent_performance_analytics",
        lambda *_args, **_kwargs: analytics,
    )

    report = crm_reporting.get_report(
        db_session,
        slug=slug,
        query=crm_reporting.CrmReportQuery(
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 31),
            person_id=person_id,
        ),
    )
    metrics = {metric.label: metric.value for metric in report.metrics}

    assert metrics["Assigned chats"] == "8"
    assert metrics["Resolved chats"] == "6"
    assert metrics["Active now"] == "2"
    assert metrics["SLA adherence"] == "SLA not configured"
    assert report.agent_rows[0].score_label == "Current evidence"


def test_agent_csv_includes_sla_and_status_fields(db_session, monkeypatch):
    monkeypatch.setattr(
        crm_reporting.team_inbox_metrics,
        "agent_performance_analytics",
        lambda *_args, **_kwargs: _analytics_page(sla=True),
    )
    report = crm_reporting.get_report(
        db_session,
        slug=crm_reporting.CrmReportSlug.AGENT_PERFORMANCE,
        query=crm_reporting.CrmReportQuery(
            date_from=date(2026, 8, 1), date_to=date(2026, 8, 31)
        ),
    )

    header = crm_reporting.build_csv(report).splitlines()[0]
    assert header == (
        "agent,team,assigned,resolved,active_now,avg_resolution,"
        "avg_first_response,first_response_sla,first_response_sla_rate,"
        "resolution_sla,resolution_sla_rate,status,score"
    )
    assert report.agent_rows[0].first_response_sla_met_count == 7
    assert report.agent_rows[0].first_response_sla_breached_count == 1


def test_agent_detail_link_preserves_period_and_team_filters():
    analytics = _analytics_page()
    view_row = crm_reporting._agent_view_row(analytics.rows[0])
    report = crm_reporting.CrmReportPage(
        definition=crm_reporting.REPORT_DEFINITIONS[
            crm_reporting.CrmReportSlug.AGENT_PERFORMANCE
        ],
        metrics=(),
        columns=("Agent", "Team"),
        rows=(),
        total=1,
        page=1,
        per_page=50,
        agent_rows=(view_row,),
    )

    rendered = report_routes.templates.env.get_template(
        "admin/reports/_agent_performance_results.html"
    ).render(
        report=report,
        detail=None,
        report_error=None,
        range_value="custom",
        date_from="2026-08-01",
        date_to="2026-08-28",
        service_team_id=str(view_row.service_team_id),
        previous_page_url=None,
        next_page_url=None,
    )

    assert f"/agent-performance/{view_row.agent_id}?range=custom" in rendered
    assert "date_from=2026-08-01" in rendered
    assert "date_to=2026-08-28" in rendered
    assert f"service_team_id={view_row.service_team_id}" in rendered
    assert "preserve_period=1" in rendered


def test_operational_templates_compile_and_main_table_stays_compact():
    for name in (
        "admin/reports/operational.html",
        "admin/reports/_agent_performance_results.html",
        "admin/reports/agent_performance_detail.html",
        "admin/reports/_agent_performance_detail_content.html",
    ):
        assert report_routes.templates.env.get_template(name)
    assert (
        "Agent",
        "Team",
        "Assigned",
        "Resolved",
        "Active now",
        "Avg first response",
        "Status / score",
    ) in crm_reporting._agent_performance.__code__.co_consts
