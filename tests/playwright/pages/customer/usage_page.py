"""Customer portal usage page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class CustomerUsagePage(BasePage):
    """Page object for the customer usage page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/customer/usage") -> None:
        """Navigate to the usage page."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the usage page is loaded."""
        expect(self.page.get_by_role("heading", name="Usage", exact=True)).to_be_visible()

    def expect_usage_chart_visible(self) -> None:
        """Assert usage chart is visible."""
        expect(self.page.locator("[data-testid='usage-chart']").or_(
            self.page.locator("canvas").or_(
                self.page.locator("svg")
            )
        ).first).to_be_visible()

    def expect_usage_summary_visible(self) -> None:
        """Assert usage summary is visible."""
        expect(self.page.locator("[data-testid='usage-summary']").or_(
            self.page.get_by_text("Total", exact=False).or_(
                self.page.get_by_text("GB", exact=False)
            )
        ).first).to_be_visible()

    def select_time_period(self, period: str) -> None:
        """Select time period for usage display."""
        self.page.get_by_label("Period").or_(
            self.page.get_by_role("combobox")
        ).first.select_option(period)

    def get_total_usage(self) -> str:
        """Get total usage value."""
        usage_element = self.page.locator("[data-testid='total-usage']").or_(
            self.page.get_by_text("GB", exact=False)
        ).first
        return usage_element.text_content() or ""

    def get_upload_usage(self) -> str:
        """Get upload usage value."""
        upload_element = self.page.locator("[data-testid='upload-usage']").or_(
            self.page.get_by_text("Upload", exact=False)
        ).first
        return upload_element.text_content() or ""

    def get_download_usage(self) -> str:
        """Get download usage value."""
        download_element = self.page.locator("[data-testid='download-usage']").or_(
            self.page.get_by_text("Download", exact=False)
        ).first
        return download_element.text_content() or ""

    def export_usage_data(self) -> None:
        """Export usage data."""
        self.page.get_by_role("button", name="Export").or_(
            self.page.get_by_role("link", name="Export")
        ).first.click()

    def view_daily_breakdown(self) -> None:
        """View daily usage breakdown."""
        self.page.get_by_role("button", name="Daily").or_(
            self.page.get_by_role("tab", name="Daily")
        ).first.click()

    def view_monthly_breakdown(self) -> None:
        """View monthly usage breakdown."""
        self.page.get_by_role("button", name="Monthly").or_(
            self.page.get_by_role("tab", name="Monthly")
        ).first.click()
