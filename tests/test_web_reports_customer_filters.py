from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

from app.models.bandwidth import BandwidthSample
from app.models.subscriber import SubscriberStatus, UserType
from app.models.usage import (
    AccountingStatus,
    RadiusAccountingSession,
    SubscriberDailyUsage,
)
from app.services import usage_summary, web_reports


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


def test_customer_report_is_visible_from_reports_hub():
    route_source = Path("app/web/admin/reports.py").read_text(encoding="utf-8")
    page_template = Path("templates/admin/reports/subscribers.html").read_text(
        encoding="utf-8"
    )

    assert '"name": "Customer Report"' in route_source
    assert '"url": "/admin/reports/customers"' in route_source
    assert '"/customers"' in route_source
    # The export is gated with the same permission as the page (customer:read).
    assert (
        '"/customers/export", '
        'dependencies=[Depends(require_permission("customer:read"))]' in route_source
    )
    assert "Customer Report - Admin" in page_template
    assert 'action="/admin/reports/customers"' in page_template
    assert 'action="/admin/reports/customers/export"' in page_template


def test_customer_report_breakdowns_scroll_only_beyond_fifteen_records():
    page_template = Path("templates/admin/reports/_subscribers_details.html").read_text(
        encoding="utf-8"
    )

    assert "grid grid-cols-1 items-start gap-6 lg:grid-cols-2" in page_template
    assert "plan_distribution|length > 15" in page_template
    assert "regional_breakdown|length > 15" in page_template
    assert (
        page_template.count(
            'style="max-height: 29.25rem; overflow-x: hidden; overflow-y: auto;"'
        )
        == 2
    )


def test_customer_growth_chart_has_readable_height_and_two_scales():
    page_template = Path("templates/admin/reports/_subscribers_details.html").read_text(
        encoding="utf-8"
    )

    assert 'id="subscriber-growth-chart"' in page_template
    assert "height: 260px; min-height: 260px;" in page_template
    assert "yAxisID: 'yTotal'" in page_template
    assert "yAxisID: 'yNew'" in page_template


def test_customer_report_lazily_loads_details_in_requested_order():
    page = Path("templates/admin/reports/subscribers.html").read_text(encoding="utf-8")
    details = Path("templates/admin/reports/_subscribers_details.html").read_text(
        encoding="utf-8"
    )
    assert 'hx-trigger="revealed once"' in page
    assert 'hx-get="/admin/reports/customers/content?' in page
    assert "Active Customers by Plan" not in page
    assert (
        details.index("Customer Growth Trend")
        < details.index("Active Customers by Plan")
        < details.index("Regional Breakdown")
    )
    assert details.count('value_size="24px"') == 2


def test_matching_customers_scrolls_beyond_fifteen_and_has_page_search():
    page_template = Path("templates/admin/reports/_subscribers_details.html").read_text(
        encoding="utf-8"
    )

    assert "customers|length > 15" in page_template
    assert "max-height: 48rem; overflow-y: auto;" in page_template
    assert 'id="matching-customers-search"' in page_template
    assert "Search displayed customers..." in page_template
    assert "data-matching-customer-row" in page_template
    assert 'id="matching-customers-no-results"' in page_template
    assert 'class="h-[43.75rem] overflow-auto"' not in page_template


def test_status_and_recent_signup_cards_use_matching_scrollable_heights():
    page_template = Path("templates/admin/reports/_subscribers_details.html").read_text(
        encoding="utf-8"
    )

    assert page_template.count('style="height: 13.02rem; overflow: hidden;"') == 2
    assert page_template.count('style="height: calc(13.02rem - 3.5625rem);"') == 2
    assert "height: 63px; min-height: 63px; max-height: 63px;" in page_template
    assert 'style="min-height: 180px;"' not in page_template
    assert 'class="h-[31rem] [&>div]:h-full"' not in page_template


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


def test_customer_report_prefers_recorded_bytes_over_samples_and_fills_history(
    db_session, subscriber, subscription
):
    subscriber.status = SubscriberStatus.active
    subscriber.user_type = UserType.customer
    subscriber.created_at = datetime(2026, 1, 10, tzinfo=UTC)
    db_session.add_all(
        (
            SubscriberDailyUsage(
                subscription_id=subscription.id,
                splynx_service_id=81201,
                usage_date=date(2026, 1, 20),
                upload_bytes=1024**3,
                download_bytes=0,
            ),
            SubscriberDailyUsage(
                subscription_id=subscription.id,
                splynx_service_id=81201,
                usage_date=date(2026, 1, 21),
                upload_bytes=2 * 1024**3,
                download_bytes=0,
            ),
            RadiusAccountingSession(
                subscription_id=subscription.id,
                session_id="report-recorded-jan20",
                status_type=AccountingStatus.stop,
                session_start=datetime(2026, 1, 20, 12, tzinfo=UTC),
                input_octets=3 * 1024**3,
                output_octets=0,
            ),
            BandwidthSample(
                subscription_id=subscription.id,
                rx_bps=4_000_000,
                tx_bps=1_000_000,
                sample_at=datetime(2026, 1, 20, 12, tzinfo=UTC),
            ),
        )
    )
    db_session.commit()

    rows = usage_summary.period_usage_by_subscriber(
        db_session,
        query=usage_summary.PeriodUsageQuery(
            subscriber_ids=(subscriber.id,),
            start=datetime(2026, 1, 20, tzinfo=UTC),
            end=datetime(2026, 1, 22, tzinfo=UTC),
        ),
    )
    assert rows[0].usage_bytes == 5 * 1024**3
    assert rows[0].source == usage_summary.PeriodUsageSource.recorded

    data = web_reports.get_subscribers_report_data(
        db_session, date_from="2026-01-01", date_to="2026-01-31"
    )
    assert data["total_usage_gb"] == 5
    assert data["usage_has_data"]
    assert not data["usage_includes_estimate"]

    db_session.add(
        BandwidthSample(
            subscription_id=subscription.id,
            rx_bps=1_000_000,
            tx_bps=0,
            sample_at=datetime(2026, 1, 22, 12, tzinfo=UTC),
        )
    )
    db_session.commit()
    mixed = usage_summary.period_usage_by_subscriber(
        db_session,
        query=usage_summary.PeriodUsageQuery(
            subscriber_ids=(subscriber.id,),
            start=datetime(2026, 1, 20, tzinfo=UTC),
            end=datetime(2026, 1, 23, tzinfo=UTC),
        ),
    )
    assert mixed[0].source == usage_summary.PeriodUsageSource.mixed
    assert mixed[0].usage_bytes > 5 * 1024**3


def test_customer_report_marks_missing_usage_as_unavailable(db_session, subscriber):
    subscriber.status = SubscriberStatus.active
    subscriber.user_type = UserType.customer
    subscriber.created_at = datetime(2026, 1, 10, tzinfo=UTC)
    db_session.commit()

    data = web_reports.get_subscribers_report_data(
        db_session, date_from="2026-01-01", date_to="2026-01-31"
    )

    assert data["total_usage_gb"] == 0
    assert not data["usage_has_data"]
    assert data["customers"][0].period_usage_source == (
        usage_summary.PeriodUsageSource.unavailable
    )


def test_customer_report_end_only_filter_uses_a_window_ending_on_that_day():
    _start, _end, shown_start, shown_end = web_reports._customer_report_usage_window(
        date_to="2026-01-31"
    )
    assert shown_start == "2026-01-02"
    assert shown_end == "2026-01-31"


def test_recent_signups_use_a_view_model_without_mutating_subscriber(
    db_session, subscriber
):
    subscriber.status = SubscriberStatus.active
    subscriber.is_active = True
    subscriber.user_type = UserType.customer
    subscriber.created_at = datetime(2026, 1, 10, tzinfo=UTC)
    db_session.commit()

    data = web_reports.get_subscribers_report_data(db_session)

    recent = next(
        row for row in data["recent_subscribers"] if row.name == subscriber.name
    )
    assert recent.derived_status == SubscriberStatus.active
    assert subscriber.status == SubscriberStatus.active
    assert not hasattr(subscriber, "derived_status")
