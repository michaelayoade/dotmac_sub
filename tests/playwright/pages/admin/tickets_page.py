"""Admin tickets page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class AdminTicketsPage(BasePage):
    """Page object for the admin tickets list."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/admin/tickets") -> None:
        super().goto(path)

    def expect_loaded(self) -> None:
        expect(
            self.page.get_by_role("heading", name="Tickets", exact=True).or_(
                self.page.get_by_text("Tickets", exact=False)
            ).first
        ).to_be_visible()


# Backwards-compatible alias used by some tests.
TicketsPage = AdminTicketsPage
