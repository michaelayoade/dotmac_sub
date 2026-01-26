"""Settings page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class SettingsPage(BasePage):
    """Page object for the system settings page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self) -> None:
        """Navigate to the settings page."""
        super().goto("/admin/system/settings")

    def expect_loaded(self) -> None:
        """Assert the settings page is loaded."""
        expect(self.page.get_by_role("heading", name="System Settings", exact=True)).to_be_visible()

    def select_domain(self, domain: str) -> None:
        """Select a settings domain."""
        self.page.get_by_label("Domain").select_option(domain)

    def fill_setting(self, key: str, value: str) -> None:
        """Fill a setting value."""
        self.page.get_by_label(key).fill(value)

    def toggle_setting(self, key: str) -> None:
        """Toggle a boolean setting."""
        self.page.get_by_label(key).click()

    def save(self) -> None:
        """Save settings."""
        self.page.get_by_role("button", name="Save").click()

    def expect_saved(self) -> None:
        """Assert settings were saved successfully."""
        expect(self.page.get_by_text("saved", exact=False)).to_be_visible()

    def expect_error(self, message: str) -> None:
        """Assert an error is displayed."""
        expect(self.page.locator(".text-red-500, .error").filter(has_text=message)).to_be_visible()

    def reset_to_defaults(self) -> None:
        """Reset settings to defaults."""
        self.page.get_by_role("button", name="Reset").click()
