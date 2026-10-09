from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.web_reports import (
    RegionalReportData,
    RegionalReportMoney,
    RegionalReportRow,
    _regional_report_window,
    build_regional_report_csv,
)

REGIONAL_REPORT_TEMPLATE = Path("templates/admin/reports/regional_performance.html")


def test_regional_report_window_treats_end_date_as_inclusive():
    start, end, effective_from, effective_to = _regional_report_window(
        date_from="2026-06-28",
        date_to="2026-08-28",
    )

    assert start == datetime(2026, 6, 28, tzinfo=UTC)
    assert end == datetime(2026, 8, 29, tzinfo=UTC)
    assert effective_from == "2026-06-28"
    assert effective_to == "2026-08-28"


def test_regional_report_window_rejects_unbounded_date_ranges():
    with pytest.raises(ValueError, match="limited to 366 days"):
        _regional_report_window(date_from="2020-01-01", date_to="2022-01-01")


def test_regional_report_csv_preserves_status_and_connection_dimensions():
    row = RegionalReportRow(
        region_id="region-1",
        name="Gudu",
        color="#0ea5e9",
        is_unassigned=False,
        total_customers=5,
        active_services=3,
        active_customers=2,
        suspended_customers=1,
        disabled_customers=0,
        canceled_customers=0,
        blocked_customers=1,
        other_customers=1,
        wireless_customers=2,
        wired_customers=2,
        unspecified_customers=1,
        money=(
            RegionalReportMoney(
                currency="NGN",
                billed=Decimal("1000"),
                collected=Decimal("800"),
                outstanding=Decimal("200"),
            ),
        ),
    )

    csv_text = build_regional_report_csv(
        RegionalReportData(
            rows=(row,),
            currency_totals=row.money,
            region_options=(),
            date_from="2026-06-28",
            date_to="2026-08-28",
            selected_region_id=row.region_id,
            configured_region_count=1,
            total_customers=row.total_customers,
            total_active_services=row.active_services,
            unassigned_customers=0,
            primary_currency="NGN",
            primary_collected_max=Decimal("800"),
        )
    )

    assert "other_customers" in csv_text.splitlines()[0]
    assert "wireless_customers" in csv_text.splitlines()[0]
    assert "Gudu,NGN,1000,800,200,5,3,2,1,0,0,1,1,2,2,1" in csv_text


def test_regional_report_links_supported_customer_drilldowns_only():
    template = REGIONAL_REPORT_TEMPLATE.read_text(encoding="utf-8")

    assert "{{ customer_filter }}&status=suspended" in template
    assert "{{ customer_filter }}&status=disabled" in template
    assert "{{ customer_filter }}&status=canceled" in template
    assert "{{ customer_filter }}&status=blocked" in template
    assert 'title="View customers in {{ row.name }}"' in template
    assert "&connection_type=" not in template
    assert "active_service=" not in template


def test_regional_report_uses_one_assignment_query_for_all_metric_groups():
    source = Path("app/services/web_reports.py").read_text(encoding="utf-8")

    assert (
        "report_stmt = union_all(status_stmt, active_stmt, invoice_stmt, payment_stmt)"
        in source
    )
    assert source.count("db.execute(report_stmt)") == 1
    assert "MAX_REGIONAL_REPORT_DAYS = 366" in source
