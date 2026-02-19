"""Customer portal support page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class CustomerSupportPage(BasePage):
    """Page object for customer support (tickets listing)."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/customer/support") -> None:
        super().goto(path)

    def expect_loaded(self) -> None:
        expect(
            self.page.get_by_role("heading", name="Support", exact=True).or_(
                self.page.get_by_text("Support", exact=False)
            ).first
        ).to_be_visible()

    def expect_tickets_visible(self) -> None:
        expect(
            self.page.locator("[data-testid='tickets']").or_(
                self.page.get_by_text("Tickets", exact=False)
            ).first
        ).to_be_visible()

    def click_new_ticket(self) -> None:
        self.page.get_by_role("button", name="New Ticket").or_(
            self.page.get_by_role("link", name="New Ticket")
        ).or_(
            self.page.get_by_role("button", name="Create Ticket")
        ).first.click()

