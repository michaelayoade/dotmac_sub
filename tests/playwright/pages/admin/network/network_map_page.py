"""Comprehensive admin network map page object."""

from __future__ import annotations

from playwright.sync_api import expect

from tests.playwright.pages.base_page import BasePage


class NetworkMapPage(BasePage):
    """Page object for the comprehensive admin network map."""

    def goto(self, path: str = "/admin/network/map") -> None:
        """Navigate to the comprehensive network map."""
        self.page.goto(
            f"{self.base_url}{path}", wait_until="domcontentloaded", timeout=30000
        )

    def expect_loaded(self) -> None:
        """Assert that the map page and layer controls are present."""
        expect(self.page.get_by_role("heading", name="Network Map")).to_be_visible()
        expect(self.page.locator("#layers-all")).to_be_visible()
        expect(self.page.locator("#layers-none")).to_be_visible()

    def click_all_layers(self) -> None:
        """Enable every base map layer."""
        self.page.locator("#layers-all").click()

    def set_layer(self, layer_id: str, enabled: bool) -> None:
        """Set one base map layer and wait for its state to settle."""
        checkbox = self.page.locator(f"#layer-{layer_id}")
        if enabled:
            checkbox.check()
        else:
            checkbox.uncheck()

    def expect_all_layers_enabled(self) -> None:
        """Assert that all non-disabled base layer checkboxes are enabled."""
        checkboxes = self.page.locator(
            ".network-map-layers input[type='checkbox']:not([disabled])"
        )
        expect(checkboxes).to_have_count(13)
        for index in range(checkboxes.count()):
            expect(checkboxes.nth(index)).to_be_checked()
