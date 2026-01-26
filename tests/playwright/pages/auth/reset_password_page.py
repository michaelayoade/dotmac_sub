"""Reset password page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class ResetPasswordPage(BasePage):
    """Page object for the reset password page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, token: str) -> None:
        """Navigate to the reset password page with token."""
        super().goto(f"/auth/reset-password?token={token}")

    def expect_loaded(self) -> None:
        """Assert the reset password page is loaded."""
        expect(self.page.get_by_role("heading", name="Reset Password", exact=True)).to_be_visible()

    def fill_password(self, password: str) -> None:
        """Fill the new password field."""
        self.page.locator("input[name='password']").first.fill(password)

    def fill_password_confirm(self, password: str) -> None:
        """Fill the confirm password field."""
        self.page.locator("input[name='password_confirm']").fill(password)

    def submit(self) -> None:
        """Click the reset password button."""
        self.page.get_by_role("button", name="Reset Password").click()

    def reset_password(self, password: str) -> None:
        """Reset password with the given new password."""
        self.fill_password(password)
        self.fill_password_confirm(password)
        self.submit()

    def expect_error(self, message: str) -> None:
        """Assert an error message is displayed."""
        error_locator = self.page.locator(".text-red-700, .text-red-200").first
        expect(error_locator).to_contain_text(message)

    def expect_passwords_mismatch_error(self) -> None:
        """Assert passwords don't match error."""
        self.expect_error("Passwords do not match")

    def expect_invalid_token_error(self) -> None:
        """Assert invalid or expired token error."""
        self.expect_error("Invalid or expired")

    def expect_redirect_to_login(self) -> None:
        """Assert redirect to login page after successful reset."""
        self.page.wait_for_url("**/auth/login**")
