"""Module-manager page object."""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class ModulesPage(BasePage):
    """Canonical module and feature-control writer."""

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/admin/system/modules") -> None:
        self.page.goto(
            f"{self.base_url}{path}", wait_until="domcontentloaded", timeout=30000
        )

    def expect_loaded(self) -> None:
        expect(
            self.page.get_by_role("heading", name="Module Manager", exact=True)
        ).to_be_visible()
        expect(self.page.get_by_role("button", name="Save Controls")).to_be_visible()

    def control(self, key: str):
        return self.page.locator(f'select[name="control__{key}"]')

    def save(self) -> None:
        self.page.get_by_role("button", name="Save Controls").click()
        expect(self.page).to_have_url(re.compile(r"/admin/system/modules\?saved=1$"))
        expect(
            self.page.get_by_text("Module settings updated.", exact=True)
        ).to_be_visible()
