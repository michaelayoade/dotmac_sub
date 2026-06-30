from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from app.models.subscriber import SubscriberStatus
from app.services import web_reports


def _subscriber(email: str, status: SubscriberStatus, created_at: datetime):
    return SimpleNamespace(
        email=email,
        status=status,
        is_active=status == SubscriberStatus.active,
        metadata_=None,
        splynx_customer_id=None,
        account_start_date=None,
        created_at=created_at,
    )


def test_customer_report_filter_uses_created_range_and_current_status():
    customers = [
        _subscriber(
            "active@example.test",
            SubscriberStatus.active,
            datetime(2026, 1, 20, tzinfo=UTC),
        ),
        _subscriber(
            "blocked@example.test",
            SubscriberStatus.blocked,
            datetime(2026, 2, 10, tzinfo=UTC),
        ),
        _subscriber(
            "old@example.test",
            SubscriberStatus.active,
            datetime(2025, 12, 31, tzinfo=UTC),
        ),
    ]

    filtered = web_reports._filter_subscribers_for_report(
        customers,
        date_from="2026-01-01",
        date_to="2026-03-31",
        status="active",
    )

    assert [customer.email for customer in filtered] == ["active@example.test"]


def test_customer_report_is_visible_from_reports_hub():
    route_source = Path("app/web/admin/reports.py").read_text(encoding="utf-8")
    page_template = Path("templates/admin/reports/subscribers.html").read_text(
        encoding="utf-8"
    )

    assert '"name": "Customer Report"' in route_source
    assert '"url": "/admin/reports/customers"' in route_source
    assert '"/customers"' in route_source
    assert '@router.get("/customers/export")' in route_source
    assert "Customer Report - Admin" in page_template
    assert 'action="/admin/reports/customers"' in page_template
    assert 'action="/admin/reports/customers/export"' in page_template
