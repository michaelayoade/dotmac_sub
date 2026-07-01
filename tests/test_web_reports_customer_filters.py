from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from app.models.bandwidth import BandwidthSample
from app.models.subscriber import SubscriberStatus, UserType
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


def test_customer_report_includes_usage_for_filtered_period(
    db_session, subscriber, subscription
):
    subscriber.first_name = "Usage"
    subscriber.last_name = "Customer"
    subscriber.status = SubscriberStatus.active
    subscriber.user_type = UserType.customer
    subscriber.created_at = datetime(2026, 1, 10, tzinfo=UTC)
    db_session.add(
        BandwidthSample(
            subscription_id=subscription.id,
            rx_bps=4_000_000,
            tx_bps=1_000_000,
            sample_at=datetime(2026, 1, 20, 12, tzinfo=UTC),
        )
    )
    db_session.commit()

    data = web_reports.get_subscribers_report_data(
        db_session,
        date_from="2026-01-01",
        date_to="2026-01-31",
        status="active",
    )

    customer = data["customers"][0]
    assert customer.email == subscriber.email
    assert customer.period_avg_mbps == 5
    assert customer.period_usage_gb > 0
    assert data["total_usage_gb"] == customer.period_usage_gb
    assert data["usage_date_from"] == "2026-01-01"
    assert data["usage_date_to"] == "2026-01-31"

    csv_content = web_reports.build_subscribers_export_csv(
        db_session,
        date_from="2026-01-01",
        date_to="2026-01-31",
        status="active",
    )
    assert "period_usage_gb,period_avg_mbps,period_active_services" in csv_content
    assert "Usage Customer" in csv_content
    assert ",5.0,1" in csv_content
