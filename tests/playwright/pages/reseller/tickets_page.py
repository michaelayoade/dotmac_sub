"""Reseller portal account tickets page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class ResellerAccountTicketsPage(BasePage):
    """Page object for the reseller account support tickets page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, account_id: str) -> None:
        """Navigate to an account's tickets page."""
        super().goto(f"/reseller/accounts/{account_id}/tickets")

    def expect_loaded(self) -> None:
        """Assert the tickets page is loaded."""
        expect(self.page.get_by_role("heading", name="Support Tickets")).to_be_visible()
