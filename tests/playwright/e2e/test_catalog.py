"""Catalog management e2e tests."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

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

    def test_services_table_fits_standard_desktop_without_horizontal_scroll(
        self, admin_page: Page, settings
    ):
        """The bounded default catalog columns fit the desktop work surface."""
        page = OffersPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        table_scroller = admin_page.locator(
            "#catalog-grid-wrapper .overflow-x-auto"
        ).first
        expect(table_scroller).to_be_visible()
        assert table_scroller.evaluate(
            "element => element.scrollWidth <= element.clientWidth"
        )

    def test_search_waits_for_submit_and_clear_restores_the_filtered_list(
        self, admin_page: Page, settings
    ):
        """Typing is local; Search and Clear each make exactly one request."""
        page = OffersPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

        catalog_requests: list[str] = []

        def capture_catalog_request(request) -> None:
            parsed = urlparse(request.url)
            if request.method == "GET" and parsed.path == "/admin/catalog":
                catalog_requests.append(request.url)

        admin_page.on("request", capture_catalog_request)
        admin_page.get_by_label("Status").select_option("active")
        search_input = admin_page.get_by_label("Search tariffs")
        search_input.fill("catalog-submit-probe")
        admin_page.wait_for_timeout(450)

        assert catalog_requests == []

        admin_page.get_by_role("button", name="Search", exact=True).click()
        expect(search_input).to_have_value("catalog-submit-probe")
        expect(admin_page).to_have_url(
            re.compile(r"[?&]search=catalog-submit-probe(?:&|$)")
        )
        assert len(catalog_requests) == 1
        submitted_query = parse_qs(urlparse(catalog_requests[0]).query)
        assert submitted_query["status"] == ["active"]
        assert submitted_query["search"] == ["catalog-submit-probe"]

        admin_page.get_by_role("button", name="Clear", exact=True).click()
        expect(admin_page.get_by_label("Search tariffs")).to_have_value("")
        assert len(catalog_requests) == 2
        cleared_query = parse_qs(
            urlparse(catalog_requests[1]).query, keep_blank_values=True
        )
        assert cleared_query["status"] == ["active"]
        assert cleared_query["search"] == [""]

    def test_enter_submits_catalog_search(self, admin_page: Page, settings):
        """Enter uses the same explicit catalog-search form submission."""
        page = OffersPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

        search_input = admin_page.get_by_label("Search tariffs")
        search_input.fill("catalog-enter-probe")
        search_input.press("Enter")

        expect(admin_page).to_have_url(
            re.compile(r"[?&]search=catalog-enter-probe(?:&|$)")
        )


class TestCatalogAPI:
    """API-level tests for catalog."""

    def test_list_offers_api(self, api_context, admin_token):
        """API should return offers list."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/offers?limit=10",
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
            "/api/v1/subscriptions?limit=10",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200
        data = response.json()
        assert "items" in data
