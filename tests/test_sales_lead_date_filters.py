"""Created-date ranges use the typed Lead owner, including navigation/recovery."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.party import Party
from app.models.sales import Lead, Pipeline, PipelineStage
from app.services import sales, web_sales
from app.web.admin.sales import router

TODAY = date(2026, 9, 15)
NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)


def _lead(
    db: Session,
    *,
    created_at: datetime,
    status: str = "new",
    active: bool = True,
    title: str = "Date filter opportunity",
    value: Decimal = Decimal("100.00"),
) -> Lead:
    party = Party(
        display_name=f"Date filter {uuid4()}", party_type="person", status="active"
    )
    db.add(party)
    db.flush()
    lead = Lead(
        party_id=party.id,
        party_bound_at=NOW,
        party_binding_source="pytest",
        party_binding_reason="Lead date range regression fixture",
        title=title,
        status=status,
        is_active=active,
        estimated_value=value,
        currency="NGN",
        created_at=created_at,
        # An old lead updated today must not enter a created-date range.
        updated_at=NOW,
    )
    db.add(lead)
    db.flush()
    return lead


def _client(db: Session) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/admin")
    app.dependency_overrides[get_db] = lambda: db
    for route in router.routes:
        for dependency in getattr(route, "dependencies", ()):
            if dependency.dependency is not None:
                app.dependency_overrides[dependency.dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("preset", "start"),
    [("last_7_days", date(2026, 9, 9)), ("last_30_days", date(2026, 8, 17))],
)
def test_presets_include_today_and_use_calendar_dates(preset: str, start: date) -> None:
    result = sales.normalize_lead_list_query(
        sales.LeadListQueryInput(
            date_preset=preset,
            date_from="1999-01-01",
            date_to="1999-01-02",
        ),
        today=TODAY,
    )
    assert result.date_preset is sales.LeadListDatePreset(preset)
    assert result.date_from == start
    assert result.date_to == TODAY


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (None, None),
        ("2026-09-01", None),
        (None, "2026-09-15"),
        ("", ""),
        ("invalid", "2026-09-15"),
        ("2026-09-01", "invalid"),
        ("2026-09-16", "2026-09-15"),
        ("2026-02-29", "2026-03-01"),
        ("0000-01-01", "2026-09-15"),
        ("2026-09-01", "9999-12-31"),
        ("2026-09-01", "10000-01-01"),
        ("20260901", "20260915"),
    ],
)
def test_invalid_custom_ranges_canonicalize_without_breaking_other_filters(
    start: str | None, end: str | None
) -> None:
    result = sales.normalize_lead_list_query(
        sales.LeadListQueryInput(
            status="qualified", date_preset="custom", date_from=start, date_to=end
        ),
        today=TODAY,
    )
    assert result.status is not None and result.status.value == "qualified"
    assert (result.date_preset, result.date_from, result.date_to) == (None, None, None)


@pytest.mark.parametrize("preset", [None, "", "all_time", "unknown"])
def test_no_valid_preset_ignores_stray_dates(preset: str | None) -> None:
    result = sales.normalize_lead_list_query(
        sales.LeadListQueryInput(
            date_preset=preset, date_from="2026-09-01", date_to="2026-09-15"
        )
    )
    assert (result.date_preset, result.date_from, result.date_to) == (None, None, None)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2024-02-29", "2024-02-29"),
        ("2025-12-31", "2026-01-01"),
        ("0001-01-01", "9999-12-30"),
    ],
)
def test_valid_custom_dates_are_typed_and_trimmed(start: str, end: str) -> None:
    result = sales.normalize_lead_list_query(
        sales.LeadListQueryInput(
            date_preset=" custom ", date_from=f" {start} ", date_to=end
        )
    )
    assert result.date_preset is sales.LeadListDatePreset.CUSTOM
    assert result.date_from == date.fromisoformat(start)
    assert result.date_to == date.fromisoformat(end)


@pytest.mark.parametrize("preset", ["last_7_days", "last_30_days"])
def test_presets_filter_exact_midnight_boundaries_and_summary(
    db_session: Session, preset: str
) -> None:
    days = 7 if preset == "last_7_days" else 30
    start = datetime.combine(TODAY - timedelta(days=days - 1), time.min, tzinfo=UTC)
    end = datetime.combine(TODAY + timedelta(days=1), time.min, tzinfo=UTC)
    _lead(db_session, created_at=start - timedelta(microseconds=1))
    first = _lead(db_session, created_at=start)
    last = _lead(db_session, created_at=end - timedelta(microseconds=1), status="won")
    _lead(db_session, created_at=end)
    _lead(db_session, created_at=NOW, active=False)
    with patch("app.services.sales.service.datetime", wraps=datetime) as clock:
        clock.now.return_value = NOW
        result = sales.leads.query(
            db_session, sales.LeadListQueryInput(date_preset=preset)
        )
    assert result.items == (last, first)
    assert result.total_count == result.summary.total_leads == 2
    assert result.summary.open_leads == result.summary.won_leads == 1
    assert result.summary.pipeline_value == Decimal("100.00")


def test_custom_range_combines_with_search_pipeline_stage_owner_source_status(
    db_session: Session,
) -> None:
    pipeline = Pipeline(name="Date filter pipeline", is_active=True)
    db_session.add(pipeline)
    db_session.flush()
    stage = PipelineStage(
        pipeline_id=pipeline.id, name="Date filter stage", order_index=1
    )
    db_session.add(stage)
    db_session.flush()
    owner = uuid4()
    expected = _lead(
        db_session, created_at=NOW, status="qualified", title="Exact date cohort"
    )
    old = _lead(
        db_session,
        created_at=NOW - timedelta(days=31),
        status="qualified",
        title="Exact date cohort",
    )
    for lead in (expected, old):
        lead.pipeline_id = pipeline.id
        lead.stage_id = stage.id
        lead.owner_agent_id = owner
        lead.lead_source = "Website"
    _lead(db_session, created_at=NOW, title="Wrong cohort")
    db_session.flush()
    result = sales.leads.query(
        db_session,
        sales.LeadListQueryInput(
            search_term="Exact date cohort",
            status="qualified",
            pipeline_id=str(pipeline.id),
            stage_id=str(stage.id),
            owner_agent_id=str(owner),
            lead_source="Website",
            date_preset="custom",
            date_from="2026-09-15",
            date_to="2026-09-15",
        ),
    )
    assert result.items == (expected,)
    assert result.total_count == result.summary.total_leads == 1


def test_legacy_list_count_and_summary_remain_all_time(db_session: Session) -> None:
    old = _lead(db_session, created_at=NOW - timedelta(days=100))
    new = _lead(db_session, created_at=NOW)
    rows = sales.leads.list(
        db_session,
        pipeline_id=None,
        stage_id=None,
        owner_agent_id=None,
        status=None,
        is_active=True,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )
    count = sales.leads.count(
        db_session,
        pipeline_id=None,
        stage_id=None,
        owner_agent_id=None,
        status=None,
        lead_source=None,
        search=None,
    )
    assert rows == [new, old]
    assert count == sales.leads.summary(db_session).total_leads == 2
    assert sales.leads.query(db_session, sales.LeadListQueryInput()).total_count == 2


def test_date_scope_survives_sort_page_size_pagination_and_retry(
    db_session: Session,
) -> None:
    for _ in range(12):
        _lead(db_session, created_at=NOW)
    context = web_sales.build_leads_list_context(
        db_session,
        status="new",
        pipeline_id=None,
        stage_id=None,
        lead_source=None,
        search="Date filter opportunity",
        page=2,
        per_page=10,
        date_preset="custom",
        date_from="2026-09-15",
        date_to="2026-09-15",
    )
    assert context["total"] == context["lead_stats"]["total"] == 12
    assert len(context["leads"]) == 2
    query = context["list_query"]
    urls = [
        query.with_page(1).url("/admin/sales/leads"),
        query.with_sort("updated_at", "asc").url("/admin/sales/leads"),
        query.with_per_page(50).url("/admin/sales/leads"),
    ]
    failure = web_sales.build_leads_failure_context(
        status="new",
        search="Date filter opportunity",
        page=2,
        per_page=10,
        date_preset="custom",
        date_from="2026-09-15",
        date_to="2026-09-15",
    )
    urls.append(failure["retry_url"])
    for url in urls:
        params = parse_qs(urlsplit(url).query)
        assert params["date_preset"] == ["custom"]
        assert params["date_from"] == params["date_to"] == ["2026-09-15"]
        assert params["status"] == ["new"]
        assert params["search"] == ["Date filter opportunity"]
    assert failure["lead_stats"]["total"] is None
    assert failure["filters_active"] is True


def test_http_custom_inputs_and_reset_render_and_invalid_range_redirects(
    db_session: Session,
) -> None:
    lead = _lead(db_session, created_at=NOW)
    client = _client(db_session)
    with (
        patch("app.web.admin.get_current_user", return_value=None),
        patch("app.web.admin.get_sidebar_stats", return_value={}),
    ):
        response = client.get(
            "/admin/sales/leads?date_preset=custom&date_from=2026-09-15&date_to=2026-09-15",
            follow_redirects=True,
        )
        invalid = client.get(
            "/admin/sales/leads?date_preset=custom&date_from=2026-09-16&date_to=2026-09-15",
            follow_redirects=True,
        )
        empty = client.get(
            "/admin/sales/leads?date_preset=custom&date_from=2020-01-01&date_to=2020-01-01",
            follow_redirects=True,
        )
    assert response.status_code == invalid.status_code == empty.status_code == 200
    assert str(lead.id) in response.text
    assert 'value="custom" selected' in response.text
    assert 'name="date_from" value="2026-09-15"' in response.text
    assert 'name="date_to" value="2026-09-15"' in response.text
    assert 'name="page" value="1"' in response.text
    assert 'href="/admin/sales/leads"' in response.text
    assert "Created dates use UTC" in response.text
    assert "date_preset" not in invalid.url.params
    assert str(lead.id) not in empty.text


@pytest.mark.parametrize(
    ("preset", "start"),
    [("last_7_days", "2026-09-09"), ("last_30_days", "2026-08-17")],
)
def test_http_presets_do_not_redirect_forever(
    db_session: Session, preset: str, start: str
) -> None:
    recent = _lead(db_session, created_at=NOW)
    old = _lead(db_session, created_at=NOW - timedelta(days=31))
    client = _client(db_session)
    with (
        patch("app.web.admin.get_current_user", return_value=None),
        patch("app.web.admin.get_sidebar_stats", return_value={}),
        patch("app.services.sales.service.datetime", wraps=datetime) as clock,
    ):
        clock.now.return_value = NOW
        response = client.get(
            f"/admin/sales/leads?date_preset={preset}", follow_redirects=True
        )
        stale = client.get(
            f"/admin/sales/leads?date_preset={preset}"
            "&date_from=2020-01-01&date_to=2020-01-02",
            follow_redirects=True,
        )
    assert response.status_code == stale.status_code == 200
    # Omitted optional dates need no redirect; explicit stale dates normalize once.
    assert len(response.history) == 0
    assert len(stale.history) == 1
    assert response.url.params["date_preset"] == preset
    assert stale.url.params["date_from"] == start
    assert stale.url.params["date_to"] == "2026-09-15"
    for rendered in (response, stale):
        assert f'name="date_from" value="{start}"' in rendered.text
        assert 'name="date_to" value="2026-09-15"' in rendered.text
        assert str(recent.id) in rendered.text
        assert str(old.id) not in rendered.text


def test_http_database_failure_preserves_dates_and_does_not_write(
    db_session: Session,
) -> None:
    _lead(db_session, created_at=NOW)
    before = db_session.query(Lead).count()
    with (
        patch("app.web.admin.get_current_user", return_value=None),
        patch("app.web.admin.get_sidebar_stats", return_value={}),
        patch(
            "app.web.admin.sales.web_sales_service.build_leads_list_context",
            side_effect=OperationalError("SELECT leads", {}, Exception("test failure")),
        ),
    ):
        response = _client(db_session).get(
            "/admin/sales/leads?status=new&date_preset=custom&date_from=2026-09-01&date_to=2026-09-15"
        )
    assert response.status_code == 200
    assert "Leads could not be loaded. No CRM data was changed." in response.text
    assert "date_preset=custom" in response.text
    assert "date_from=2026-09-01" in response.text
    assert "date_to=2026-09-15" in response.text
    assert "Unavailable" in response.text
    assert db_session.query(Lead).count() == before
