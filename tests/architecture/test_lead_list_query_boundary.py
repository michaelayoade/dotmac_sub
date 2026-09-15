"""Keep the typed, unique Lead list query at the authoritative sales owner."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_lead_list_uses_one_typed_owner_query_without_full_row_distinct():
    sales = _source("app/services/sales/service.py")
    leads = sales.split("class Leads(", 1)[1].split("class Quotes(", 1)[0]
    web = _source("app/services/web_sales.py")
    context = web.split("def build_leads_list_context(", 1)[1].split(
        "def build_leads_failure_context(", 1
    )[0]

    assert "class LeadListQueryInput" in sales
    assert "class LeadListQueryResult" in sales
    assert "def _lead_list_predicates(" in sales
    assert "def _lead_search_predicate(" in sales
    assert ".correlate(Lead)" in sales
    assert ".distinct()" not in leads
    assert "sales_service.leads.query(" in context
    assert "sales_service.leads.list(" not in context
    assert "sales_service.leads.count(" not in context


def test_admin_route_remains_a_transport_adapter_for_lead_queries():
    route = _source("app/web/admin/sales.py")
    section = route.split("def leads_list(", 1)[1].split("def lead_new(", 1)[0]

    assert "build_leads_list_context(" in section
    assert ".query(" not in section
    assert ".filter(" not in section
    assert ".distinct(" not in section


def test_lead_date_range_rules_and_retry_normalization_have_one_owner():
    sales = _source("app/services/sales/service.py")
    normalizer = sales.split("def normalize_lead_list_query(", 1)[1].split(
        "def _normalize_lead_list_query(", 1
    )[0]
    predicates = sales.split("def _lead_list_predicates(", 1)[1].split(
        "def _lead_list_filters(", 1
    )[0]
    web = _source("app/services/web_sales.py")
    failure = web.split("def build_leads_failure_context(", 1)[1].split(
        "def build_lead_detail_context(", 1
    )[0]
    assert "class LeadListDatePreset" in sales
    assert "Lead.created_at >= filters.created_from" in predicates
    assert "Lead.created_at < filters.created_to_exclusive" in predicates
    assert "Lead.updated_at" not in predicates
    assert ".query(" not in normalizer
    assert "sales_service.normalize_lead_list_query(" in failure
    assert "date.fromisoformat" not in failure
    assert "timedelta(" not in failure
    assert ".query(" not in failure
    assert "created_from: datetime | None = None" in sales
    assert "created_to_exclusive: datetime | None = None" in sales
