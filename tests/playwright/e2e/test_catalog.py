"""Catalog management e2e tests."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.admin.catalog import OffersPage


class TestServices:
    """Tests for the services (tariffs) page."""

    def test_services_page_loads(self, admin_page: Page, settings):
        """Services page should load."""
        page = OffersPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_services_table_visible(self, admin_page: Page, settings):
        """Tariffs table should be visible."""
        page = OffersPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        expect(admin_page.locator("table")).to_be_visible()


class TestCatalogAPI:
    """API-level tests for catalog."""

    def test_list_offers_api(self, api_context, admin_token):
        """API should return offers list."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/catalog/offers?limit=10",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200
        data = response.json()
        assert "items" in data

    def test_list_subscriptions_api(self, api_context, admin_token):
        """API should return subscriptions list."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/catalog/subscriptions?limit=10",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200
        data = response.json()
        assert "items" in data
