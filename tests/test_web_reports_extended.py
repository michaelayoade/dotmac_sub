from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from app.services import web_reports_extended


def test_bandwidth_total_bps_expr_casts_columns_before_addition():
    expr = web_reports_extended._bandwidth_total_bps_expr()
    sql = str(
        select(func.avg(expr))
        .order_by(func.avg(expr).desc())
        .compile(dialect=postgresql.dialect())
    )

    assert "CAST(bandwidth_samples.rx_bps AS BIGINT)" in sql
    assert "CAST(bandwidth_samples.tx_bps AS BIGINT)" in sql


def test_report_window_uses_inclusive_date_inputs():
    start, end, date_from, date_to = web_reports_extended._resolve_report_window(
        date_from="2026-01-15",
        date_to="2026-03-31",
    )

    assert start.isoformat() == "2026-01-15T00:00:00+00:00"
    assert end.isoformat() == "2026-04-01T00:00:00+00:00"
    assert date_from == "2026-01-15"
    assert date_to == "2026-03-31"


def test_bandwidth_export_contains_filtered_usage_by_plan():
    content = web_reports_extended.build_bandwidth_report_export_csv(
        {
            "date_from": "2026-01-01",
            "date_to": "2026-03-31",
            "total_gb": 300,
            "active_subscribers": 12,
            "usage_by_plan": [
                {
                    "name": "Unlimited Basic",
                    "usage_gb": 300,
                    "avg_mbps": 9.25,
                    "subscribers": 8,
                }
            ],
            "top_consumers": [
                {
                    "subscriber": "Jane Customer",
                    "plan": "Unlimited Basic",
                    "usage_gb": 42,
                    "avg_mbps": 1.29,
                }
            ],
        }
    )

    assert "2026-01-01" in content
    assert "Unlimited Basic,300,9.25,8" in content
    assert "Jane Customer,Unlimited Basic,42,1.29" in content
