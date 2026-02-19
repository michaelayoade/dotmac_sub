"""Audit log page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class AuditPage(BasePage):
    """Page object for the audit log page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)
    def goto(self, path: str = "/admin/system/audit") -> None:
        """Navigate to the audit log."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the audit page is loaded."""
        expect(self.page.get_by_role("heading", name="Audit Log", exact=True)).to_be_visible()

    def search(self, query: str) -> None:
        """Search audit logs."""
        search_input = self.page.get_by_placeholder("Search")
        search_input.fill(query)
        self.page.keyboard.press("Enter")

    def filter_by_action(self, action: str) -> None:
        """Filter by action type."""
        self.page.get_by_label("Action").select_option(action)

    def filter_by_entity(self, entity_type: str) -> None:
        """Filter by entity type."""
        self.page.get_by_label("Entity").select_option(entity_type)

    def filter_by_date_range(self, start: str, end: str) -> None:
        """Filter by date range."""
        self.page.get_by_label("Start").fill(start)
        self.page.get_by_label("End").fill(end)

    def click_event_row(self, event_id: str) -> None:
        """Click on an audit event row."""
        self.page.get_by_role("row").filter(has_text=event_id).click()

    def get_event_count(self) -> int:
        """Get the count of audit events in the table."""
        rows = self.page.locator("tbody tr")
        return rows.count()

    def export_logs(self) -> None:
        """Click export button."""
        self.page.get_by_role("button", name="Export").click()
