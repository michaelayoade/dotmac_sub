"""Reseller portal profile and MFA setup page objects."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class ResellerProfilePage(BasePage):
    """Page object for the reseller profile settings page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/reseller/profile") -> None:
        """Navigate to the profile settings page."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the profile page is loaded."""
        expect(
            self.page.get_by_role("heading", name="Profile Settings")
        ).to_be_visible()

    def fill_contact_email(self, value: str) -> None:
        self.page.locator("#contact_email").fill(value)

    def fill_contact_phone(self, value: str) -> None:
        self.page.locator("#contact_phone").fill(value)

    def fill_notes(self, value: str) -> None:
        self.page.locator("#notes").fill(value)

    def save(self) -> None:
        """Submit the profile form."""
        self.page.locator(
            "form[action='/reseller/profile'] button[type='submit']"
        ).click()

    def update_contact(
        self,
        email: str | None = None,
        phone: str | None = None,
        notes: str | None = None,
    ) -> None:
        """Fill and submit the profile form."""
        if email is not None:
            self.fill_contact_email(email)
        if phone is not None:
            self.fill_contact_phone(phone)
        if notes is not None:
            self.fill_notes(notes)
        self.save()


class ResellerMfaSetupPage(BasePage):
    """Page object for the reseller MFA setup page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/reseller/profile/mfa/setup") -> None:
        """Navigate to the MFA setup page."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the MFA setup page is loaded with a confirmation form."""
        expect(self.page.get_by_role("heading", name="Set Up MFA")).to_be_visible()
        expect(self.page.locator("#code")).to_be_visible()
        expect(self.page.get_by_role("button", name="Enable MFA")).to_be_visible()

    def submit_code(self, code: str) -> None:
        """Submit a verification code to confirm MFA."""
        self.page.locator("#code").fill(code)
        self.page.get_by_role("button", name="Enable MFA").click()
