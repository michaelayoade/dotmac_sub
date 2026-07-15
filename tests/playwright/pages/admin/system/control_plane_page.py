"""Control-plane page object."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.base_page import BasePage


class ControlPlanePage(BasePage):
    """Read-only effective-state control plane."""

    SECTION_IDS = (
        "settings",
        "rbac",
        "sessions",
        "scheduler",
        "secrets",
        "integrations",
        "webhooks",
    )

    def __init__(self, page: Page, base_url: str) -> None:
        super().__init__(page, base_url)

    def goto(self, path: str = "/admin/system/control-plane") -> None:
        self.page.goto(
            f"{self.base_url}{path}", wait_until="domcontentloaded", timeout=30000
        )

    def expect_loaded(self) -> None:
        expect(
            self.page.get_by_role("heading", name="Control Plane", exact=True)
        ).to_be_visible()
        expect(self.page.locator(".control-plane-section")).to_have_count(
            len(self.SECTION_IDS)
        )
        for section_id in self.SECTION_IDS:
            expect(self.page.locator(f"section#{section_id}")).to_be_visible()

    def filter(self, query: str) -> None:
        self.page.locator("#control-plane-search").fill(query)
