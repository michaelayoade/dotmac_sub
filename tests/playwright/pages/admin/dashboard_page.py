"""Dashboard page object."""

from __future__ import annotations

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class AdminDashboardPage(BasePage):
    """Page object for the admin dashboard."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/admin/dashboard") -> None:
        """Navigate to the dashboard."""
        last_error: Exception | None = None
        for candidate in (path, "/admin"):
            for _ in range(2):
                try:
                    self.page.goto(
                        f"{self.base_url}{candidate}",
                        wait_until="commit",
                        timeout=30000,
                    )
                    self.page.wait_for_load_state("domcontentloaded")
                    return
                except PlaywrightError as exc:
                    last_error = exc
        if last_error:
            raise last_error

    def expect_loaded(self) -> None:
        """Assert the dashboard is loaded."""
        expect(
            self.page.get_by_role("heading", name="Infrastructure Overview", exact=True)
        ).to_be_visible()

    def expect_stats_visible(self) -> None:
        """Assert stats cards are visible."""
        expect(self.page.get_by_text("Subscribers").first).to_be_visible()

    def expect_activity_feed_visible(self) -> None:
        """Assert activity feed section is visible."""
        expect(self.page.get_by_text("Recent Events").first).to_be_visible()

    def search(self, query: str) -> None:
        """Use the global search."""
        search_input = self.page.get_by_placeholder("Search")
        if search_input.is_visible():
            search_input.fill(query)
            self.page.keyboard.press("Enter")

    def click_nav_link(self, name: str) -> None:
        """Click a navigation link in the sidebar."""
        self.page.get_by_role("link", name=name).first.click()

    def expect_sidebar_visible(self) -> None:
        """Assert the sidebar navigation is visible."""
        expect(
            self.page.locator("nav, aside, .sidebar, [data-testid='sidebar']").first
        ).to_be_visible()

    def click_subscribers_link(self) -> None:
        """Click the link to customers page."""
        self.page.get_by_role("link", name="Customers").first.click()

    def click_tickets_link(self) -> None:
        """Click the link to tickets page."""
        self.page.get_by_role("link", name="Support").first.click()

    def click_billing_link(self) -> None:
        """Click the link to billing page."""
        self.page.get_by_role("link", name="Billing").first.click()
