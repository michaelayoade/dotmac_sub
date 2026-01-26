"""Roles list page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class RolesPage(BasePage):
    """Page object for the system roles list page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self) -> None:
        """Navigate to the roles list."""
        super().goto("/admin/system/roles")

    def expect_loaded(self) -> None:
        """Assert the roles page is loaded."""
        expect(self.page.get_by_role("heading", name="Roles & Permissions", exact=True)).to_be_visible()

    def click_new_role(self) -> None:
        """Click new role button."""
        self.page.get_by_role("link", name="New Role").click()

    def click_role_row(self, role_name: str) -> None:
        """Click on a role row."""
        self.page.get_by_role("row").filter(has_text=role_name).click()

    def expect_role_in_list(self, role_name: str) -> None:
        """Assert a role is visible in the list."""
        expect(self.page.get_by_role("row").filter(has_text=role_name)).to_be_visible()

    def get_role_count(self) -> int:
        """Get the count of roles in the table."""
        rows = self.page.locator("tbody tr")
        return rows.count()
