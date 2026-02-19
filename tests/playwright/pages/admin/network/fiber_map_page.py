"""Fiber Map page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class FiberMapPage(BasePage):
    """Page object for the fiber network map page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)
    def goto(self, path: str = "/admin/network/fiber-map") -> None:
        """Navigate to the fiber map page."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the fiber map page is loaded."""
        expect(self.page.get_by_role("heading", name="Fiber", exact=True)).to_be_visible()

    def expect_map_visible(self) -> None:
        """Assert the map component is visible."""
        expect(self.page.locator("#map, .leaflet-container, [data-testid='fiber-map']").first).to_be_visible()

    def zoom_in(self) -> None:
        """Zoom in on the map."""
        self.page.locator(".leaflet-control-zoom-in, [aria-label='Zoom in']").click()

    def zoom_out(self) -> None:
        """Zoom out on the map."""
        self.page.locator(".leaflet-control-zoom-out, [aria-label='Zoom out']").click()

    def click_layer_toggle(self, layer_name: str) -> None:
        """Toggle a map layer."""
        self.page.get_by_label(layer_name).click()

    def search_location(self, query: str) -> None:
        """Search for a location on the map."""
        search = self.page.get_by_placeholder("Search")
        search.fill(query)
        self.page.keyboard.press("Enter")

    def click_feature(self, feature_id: str) -> None:
        """Click on a map feature."""
        self.page.locator(f"[data-feature-id='{feature_id}']").click()

    def expect_popup_visible(self) -> None:
        """Assert a popup is visible on the map."""
        expect(self.page.locator(".leaflet-popup, .map-popup")).to_be_visible()
