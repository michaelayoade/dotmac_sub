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
        admin_page.locator("#catalog-grid-search-form").get_by_label(
            "Status"
        ).select_option("active")
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

        admin_page.get_by_role("button", name="Clear search", exact=True).click()
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

    def test_profile_selection_only_warns_and_does_not_change_tariff_speed(
        self, admin_page: Page, settings
    ):
        admin_page.goto(f"{settings.base_url}/admin/catalog/offers/create")
        download = admin_page.locator("#speed_download_mbps")
        upload = admin_page.locator("#speed_upload_mbps")
        download.fill("10")
        upload.fill("5")
        admin_page.locator("#radius_profile_id").evaluate(
            """select => {
                const option = new Option('Profile preview', 'preview-only');
                option.dataset.downloadMbps = '20.0';
                option.dataset.uploadMbps = '5';
                option.dataset.rateSource = 'mikrotik_rate_limit';
                option.dataset.rateNote = 'Its literal rate limit takes precedence when provisioning.';
                option.dataset.blocksMismatch = 'true';
                select.add(option);
                select.value = option.value;
                select.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        expect(download).to_have_value("10")
        expect(upload).to_have_value("5")
        notice = admin_page.locator("#profile-speed-notice")
        expect(notice).to_contain_text("Profile speed: 20.0/5 Mbps")
        expect(notice).to_contain_text("literal rate limit takes precedence")
        expect(notice).to_contain_text("speeds differ")

    def test_category_tabs_keep_submitted_filters(self, admin_page: Page, settings):
        page = OffersPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        admin_page.locator("#catalog-grid-search-form").get_by_label(
            "Status"
        ).select_option("active")
        admin_page.get_by_label("Search tariffs").fill("fiber")
        admin_page.get_by_role("button", name="Search", exact=True).click()
        tab = admin_page.locator('[data-catalog-category="internet"]')
        expect(tab).to_have_attribute("href", re.compile(r"status=active"))
        expect(tab).to_have_attribute("href", re.compile(r"search=fiber"))
        tab.click()
        expect(admin_page).to_have_url(re.compile(r"[?&]plan_category=internet"))
        expect(admin_page).to_have_url(re.compile(r"[?&]status=active"))


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
