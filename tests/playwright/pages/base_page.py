"""Base page object class with common locators and methods."""

from __future__ import annotations

from playwright.sync_api import Page, expect


class BasePage:
    """Base class for all page objects."""

    def __init__(self, page: Page, base_url: str) -> None:
        self.page = page
        self.base_url = base_url

    def goto(self, path: str = "") -> None:
        """Navigate to a path relative to base URL."""
        url = f"{self.base_url}{path}" if path else self.base_url
        self.page.goto(url)

    def wait_for_load(self) -> None:
        """Wait for page to finish loading."""
        self.page.wait_for_load_state("domcontentloaded")

    def get_page_title(self) -> str:
        """Get the page title."""
        return self.page.title()

    def expect_url_contains(self, path: str) -> None:
        """Assert URL contains the given path."""
        expect(self.page).to_have_url(f"**{path}**")

    def expect_heading(self, text: str) -> None:
        """Assert a heading with the given text is visible."""
        expect(self.page.get_by_role("heading", name=text)).to_be_visible()

    def expect_text_visible(self, text: str) -> None:
        """Assert text is visible on the page."""
        expect(self.page.get_by_text(text)).to_be_visible()

    def expect_error_message(self, message: str) -> None:
        """Assert an error message is displayed."""
        # Common error display patterns
        error_locator = self.page.locator(
            f".text-red-700:has-text('{message}'), "
            f".text-red-500:has-text('{message}'), "
            f"[role='alert']:has-text('{message}')"
        ).first
        expect(error_locator).to_be_visible()

    def expect_success_message(self, message: str) -> None:
        """Assert a success message is displayed."""
        success_locator = self.page.locator(
            f".text-green-700:has-text('{message}'), "
            f".text-green-500:has-text('{message}'), "
            f"[role='status']:has-text('{message}')"
        ).first
        expect(success_locator).to_be_visible()

    def click_button(self, name: str) -> None:
        """Click a button by name."""
        self.page.get_by_role("button", name=name).click()

    def click_link(self, name: str) -> None:
        """Click a link by name."""
        self.page.get_by_role("link", name=name).click()

    def fill_input(self, label: str, value: str) -> None:
        """Fill an input field by label."""
        self.page.get_by_label(label).fill(value)

    def select_option(self, label: str, value: str) -> None:
        """Select an option from a dropdown by label."""
        self.page.get_by_label(label).select_option(value=value)

    def check_checkbox(self, label: str) -> None:
        """Check a checkbox by label."""
        self.page.get_by_label(label).check()

    def uncheck_checkbox(self, label: str) -> None:
        """Uncheck a checkbox by label."""
        self.page.get_by_label(label).uncheck()

    def is_checkbox_checked(self, label: str) -> bool:
        """Check if a checkbox is checked."""
        return self.page.get_by_label(label).is_checked()

    def get_table_rows(self, table_locator: str = "table") -> int:
        """Get the number of rows in a table."""
        return self.page.locator(f"{table_locator} tbody tr").count()

    def expect_table_has_rows(self, min_rows: int = 1, table_locator: str = "table") -> None:
        """Assert table has at least the specified number of rows."""
        rows = self.page.locator(f"{table_locator} tbody tr")
        expect(rows).to_have_count(min_rows, timeout=10000)

    def wait_for_navigation(self, url_pattern: str) -> None:
        """Wait for navigation to a URL matching the pattern."""
        self.page.wait_for_url(url_pattern)

    def get_current_url(self) -> str:
        """Get the current page URL."""
        return self.page.url

    def has_cookie(self, name: str) -> bool:
        """Check if a cookie exists."""
        cookies = self.page.context.cookies()
        return any(c["name"] == name for c in cookies)

    def get_cookie(self, name: str) -> dict | None:
        """Get a cookie by name."""
        cookies = self.page.context.cookies()
        for cookie in cookies:
            if cookie["name"] == name:
                return cookie
        return None
