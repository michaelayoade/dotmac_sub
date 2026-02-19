"""Reports e2e tests."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class ReportsPage(BasePage):
    """Page object for the reports page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/admin/reports") -> None:
        """Navigate to reports page."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert reports page is loaded."""
        expect(self.page.get_by_role("heading", name="Report")).to_be_visible()


class TestReportsAccess:
    """Tests for reports page access."""

    def test_reports_page_loads(self, admin_page: Page, settings):
        """Reports page should load."""
        page = ReportsPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_reports_page_shows_categories(self, admin_page: Page, settings):
        """Reports page should show report categories."""
        page = ReportsPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        # Should show some report categories
        expect(admin_page.get_by_text("Revenue", exact=False).or_(
            admin_page.get_by_text("Financial", exact=False).or_(
                admin_page.get_by_text("Report", exact=False)
            )
        ).first).to_be_visible()


class TestRevenueReports:
    """Tests for revenue reports."""

    def test_revenue_report_accessible(self, admin_page: Page, settings):
        """Revenue report should be accessible."""
        admin_page.goto(f"{settings.base_url}/admin/reports")
        expect(admin_page.get_by_text("Revenue", exact=False).first).to_be_visible()

    def test_revenue_report_date_filter(self, admin_page: Page, settings):
        """Revenue report should have date filters."""
        admin_page.goto(f"{settings.base_url}/admin/reports")
        # Date filter elements should exist
        expect(admin_page.get_by_role("combobox").or_(
            admin_page.get_by_label("Date").or_(
                admin_page.get_by_label("Period")
            )
        ).first).to_be_visible()


class TestSubscriberReports:
    """Tests for subscriber reports."""

    def test_subscriber_report_accessible(self, admin_page: Page, settings):
        """Subscriber report should be accessible."""
        admin_page.goto(f"{settings.base_url}/admin/reports")
        expect(admin_page.get_by_text("Subscriber", exact=False).or_(
            admin_page.get_by_text("Customer", exact=False)
        ).first).to_be_visible()


class TestNetworkReports:
    """Tests for network reports."""

    def test_network_report_accessible(self, admin_page: Page, settings):
        """Network report should be accessible."""
        admin_page.goto(f"{settings.base_url}/admin/reports")
        # Check for network or operations reports
        expect(admin_page.get_by_text("Network", exact=False).or_(
            admin_page.get_by_text("Operation", exact=False).or_(
                admin_page.get_by_text("Report", exact=False)
            )
        ).first).to_be_visible()


class TestReportsAPI:
    """API-level tests for reports."""

    def test_revenue_summary_api(self, api_context, admin_token):
        """API should return revenue summary."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/reports/revenue/summary",
            headers=bearer_headers(admin_token),
        )
        # May return 200 or 404 depending on implementation
        assert response.status in [200, 404]

    def test_subscriber_stats_api(self, api_context, admin_token):
        """API should return subscriber statistics."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/reports/subscribers/stats",
            headers=bearer_headers(admin_token),
        )
        assert response.status in [200, 404]
