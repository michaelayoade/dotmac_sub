"""VLANs list page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class VLANsPage(BasePage):
    """Page object for the VLANs list page."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)
    def goto(self, path: str = "/admin/network/vlans") -> None:
        """Navigate to the VLANs list."""
        super().goto(path)

    def expect_loaded(self) -> None:
        """Assert the VLANs page is loaded."""
        expect(self.page.get_by_role("heading", name="VLAN", exact=True)).to_be_visible()

    def click_new_vlan(self) -> None:
        """Click new VLAN button."""
        self.page.get_by_role("link", name="New").first.click()

    def click_vlan_row(self, vlan_id: str) -> None:
        """Click on a VLAN row."""
        self.page.get_by_role("row").filter(has_text=vlan_id).click()

    def expect_vlan_in_list(self, vlan_id: str) -> None:
        """Assert a VLAN is visible in the list."""
        expect(self.page.get_by_role("row").filter(has_text=vlan_id)).to_be_visible()

    def get_vlan_count(self) -> int:
        """Get the count of VLANs in the table."""
        rows = self.page.locator("tbody tr")
        return rows.count()
