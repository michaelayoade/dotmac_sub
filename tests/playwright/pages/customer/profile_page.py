"""Customer portal profile page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class CustomerProfilePage(BasePage):
    """Page object for the customer profile page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/customer/profile") -> None:
        """Navigate to the profile page."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the profile page is loaded."""
        expect(self.page.get_by_role("heading", name="Profile", exact=True)).to_be_visible()

    def expect_personal_info_visible(self) -> None:
        """Assert personal info section is visible."""
        expect(self.page.get_by_text("Name", exact=False).or_(
            self.page.get_by_text("Email", exact=False)
        ).first).to_be_visible()

    def expect_contact_info_visible(self) -> None:
        """Assert contact info section is visible."""
        expect(self.page.get_by_text("Phone", exact=False).or_(
            self.page.get_by_text("Address", exact=False)
        ).first).to_be_visible()

    def click_edit_profile(self) -> None:
        """Click edit profile button."""
        self.page.get_by_role("button", name="Edit").first.click()

    def fill_first_name(self, first_name: str) -> None:
        """Fill first name."""
        self.page.get_by_label("First").fill(first_name)

    def fill_last_name(self, last_name: str) -> None:
        """Fill last name."""
        self.page.get_by_label("Last").fill(last_name)

    def fill_email(self, email: str) -> None:
        """Fill email address."""
        self.page.get_by_label("Email").fill(email)

    def fill_phone(self, phone: str) -> None:
        """Fill phone number."""
        self.page.get_by_label("Phone").fill(phone)

    def save_profile(self) -> None:
        """Save profile changes."""
        self.page.get_by_role("button", name="Save").first.click()

    def expect_profile_saved(self) -> None:
        """Assert profile was saved successfully."""
        expect(self.page.get_by_text("saved", exact=False).or_(
            self.page.get_by_text("updated", exact=False)
        ).first).to_be_visible()

    def click_change_password(self) -> None:
        """Click change password button."""
        self.page.get_by_role("button", name="Password").or_(
            self.page.get_by_role("link", name="Password")
        ).first.click()

    def fill_current_password(self, password: str) -> None:
        """Fill current password."""
        self.page.get_by_label("Current").fill(password)

    def fill_new_password(self, password: str) -> None:
        """Fill new password."""
        self.page.get_by_label("New Password").fill(password)

    def fill_confirm_password(self, password: str) -> None:
        """Fill confirm password."""
        self.page.get_by_label("Confirm").fill(password)

    def submit_password_change(self) -> None:
        """Submit password change."""
        self.page.get_by_role("button", name="Change").or_(
            self.page.get_by_role("button", name="Update")
        ).first.click()

    def expect_password_changed(self) -> None:
        """Assert password was changed successfully."""
        expect(self.page.get_by_text("changed", exact=False).or_(
            self.page.get_by_text("updated", exact=False)
        ).first).to_be_visible()

    def manage_notifications(self) -> None:
        """Open notification settings."""
        self.page.get_by_role("link", name="Notification").or_(
            self.page.get_by_role("button", name="Notification")
        ).first.click()

    def toggle_email_notifications(self) -> None:
        """Toggle email notifications."""
        self.page.get_by_label("Email notification").or_(
            self.page.get_by_role("switch").first
        ).click()

    def toggle_sms_notifications(self) -> None:
        """Toggle SMS notifications."""
        self.page.get_by_label("SMS notification").or_(
            self.page.get_by_role("switch").nth(1)
        ).click()
