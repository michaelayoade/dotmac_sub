from __future__ import annotations

from playwright.sync_api import Page, expect


class AdminLoginPage:
    def __init__(self, page: Page, base_url: str) -> None:
        self.page = page
        self.base_url = base_url

    def goto(self) -> None:
        self.page.goto(f"{self.base_url}/auth/login", wait_until="domcontentloaded")

    def login(self, username: str, password: str) -> None:
        self.page.get_by_label("Email or Username").fill(username)
        self.page.get_by_label("Password").fill(password)
        self.page.get_by_role("button", name="Sign in").click()

    def expect_loaded(self) -> None:
        expect(self.page.get_by_role("heading", name="Welcome back", exact=True)).to_be_visible()
