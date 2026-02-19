"""MFA verification page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class MFAPage(BasePage):
    """Page object for the MFA verification page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)
    def goto(self, path: str = "/auth/mfa") -> None:
        """Navigate to the MFA page."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the MFA page is loaded."""
        expect(self.page.get_by_role("heading", name="Two-Factor Authentication", exact=True)).to_be_visible()

    def fill_code(self, code: str) -> None:
        """Fill the verification code."""
        self.page.get_by_label("Verification Code").fill(code)

    def submit(self) -> None:
        """Click the verify button."""
        self.page.get_by_role("button", name="Verify").click()

    def verify(self, code: str) -> None:
        """Enter and submit the verification code."""
        self.fill_code(code)
        self.submit()

    def expect_error(self, message: str = "Invalid verification code") -> None:
        """Assert an error message is displayed."""
        error_locator = self.page.locator(".text-red-700, .text-red-200").first
        expect(error_locator).to_contain_text(message)

    def expect_redirect_to_dashboard(self) -> None:
        """Assert redirect to admin dashboard after verification."""
        self.page.wait_for_url("**/admin/dashboard**")

    def click_use_recovery_code(self) -> None:
        """Click the use recovery code link."""
        self.page.get_by_role("link", name="Use a recovery code").click()

    def click_back_to_login(self) -> None:
        """Click the back to login link."""
        self.page.get_by_role("link", name="Back to sign in").click()

    def has_mfa_pending_cookie(self) -> bool:
        """Check if the mfa_pending cookie exists."""
        return self.has_cookie("mfa_pending")
