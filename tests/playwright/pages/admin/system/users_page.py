"""Users list page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class UsersPage(BasePage):
    """Page object for the system users list page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)
    def goto(self, path: str = "/admin/system/users") -> None:
        """Navigate to the users list."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the users page is loaded."""
        expect(self.page.get_by_role("heading", name="Users", exact=True)).to_be_visible()

    def search(self, query: str) -> None:
        """Search users."""
        search_input = self.page.get_by_placeholder("Search")
        search_input.fill(query)
        self.page.keyboard.press("Enter")

    def click_new_user(self) -> None:
        """Click new user button."""
        self.page.get_by_role("link", name="New User").click()

    def click_user_row(self, username: str) -> None:
        """Click on a user row."""
        self.page.get_by_role("row").filter(has_text=username).click()

    def expect_user_in_list(self, username: str) -> None:
        """Assert a user is visible in the list."""
        expect(self.page.get_by_role("row").filter(has_text=username)).to_be_visible()

    def get_user_count(self) -> int:
        """Get the count of users in the table."""
        rows = self.page.locator("tbody tr")
        return rows.count()
