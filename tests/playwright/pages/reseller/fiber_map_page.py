"""Reseller portal fiber map page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class ResellerFiberMapPage(BasePage):
    """Page object for the reseller fiber map page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/reseller/fiber-map") -> None:
        """Navigate to the fiber map."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the fiber map page is loaded."""
        expect(self.page.get_by_role("heading", name="Fiber Map")).to_be_visible()
