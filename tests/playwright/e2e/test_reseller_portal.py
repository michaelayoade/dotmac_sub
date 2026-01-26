"""Reseller portal e2e tests."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.playwright.pages.reseller import (
    ResellerLoginPage,
    ResellerDashboardPage,
    ResellerAccountsPage,
)


class TestResellerLogin:
    """Tests for reseller portal login."""

    def test_reseller_login_page_loads(self, anon_page: Page, settings):
        """Reseller login page should load."""
        page = ResellerLoginPage(anon_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_reseller_login_requires_credentials(self, anon_page: Page, settings):
        """Login should require valid credentials."""
        page = ResellerLoginPage(anon_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        # Should stay on login page without credentials
        expect(anon_page).to_have_url("**/reseller/auth/login**")


class TestResellerDashboard:
    """Tests for reseller portal dashboard."""

    def test_dashboard_loads(self, reseller_page: Page, settings):
        """Reseller dashboard should load."""
        page = ResellerDashboardPage(reseller_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_dashboard_shows_summary(self, reseller_page: Page, settings):
        """Dashboard should show summary statistics."""
        page = ResellerDashboardPage(reseller_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_summary_visible()


class TestResellerAccounts:
    """Tests for reseller accounts page."""

    def test_accounts_page_loads(self, reseller_page: Page, settings):
        """Accounts page should load."""
        page = ResellerAccountsPage(reseller_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_accounts_list_visible(self, reseller_page: Page, settings):
        """Accounts list should be visible."""
        page = ResellerAccountsPage(reseller_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        # Table should exist
        expect(reseller_page.locator("table")).to_be_visible()
