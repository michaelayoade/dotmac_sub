"""Network management e2e tests."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.admin.network import (
    FiberMapPage,
    IPManagementPage,
    OLTsPage,
    ONTsPage,
    VLANsPage,
)


class TestOLTs:
    """Tests for the OLT devices page."""

    def test_olts_page_loads(self, admin_page: Page, settings):
        """OLTs page should load."""
        page = OLTsPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_olts_table_visible(self, admin_page: Page, settings):
        """OLTs table should be visible."""
        page = OLTsPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        expect(admin_page.locator("table")).to_be_visible()

    def test_new_olt_button(self, admin_page: Page, settings):
        """New OLT button should navigate to form."""
        page = OLTsPage(admin_page, settings.base_url)
        page.goto()
        page.click_new_olt()
        admin_page.wait_for_url("**/olts/new**")


class TestONTs:
    """Tests for the ONT devices page."""

    def test_onts_page_loads(self, admin_page: Page, settings):
        """ONTs page should load."""
        page = ONTsPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_onts_table_visible(self, admin_page: Page, settings):
        """ONTs table should be visible."""
        page = ONTsPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        expect(admin_page.locator("table")).to_be_visible()

    def test_new_ont_button(self, admin_page: Page, settings):
        """New ONT button should navigate to form."""
        page = ONTsPage(admin_page, settings.base_url)
        page.goto()
        page.click_new_ont()
        admin_page.wait_for_url("**/onts/new**")


class TestVLANs:
    """Tests for the VLANs page."""

    def test_vlans_page_loads(self, admin_page: Page, settings):
        """VLANs page should load."""
        page = VLANsPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_vlans_table_visible(self, admin_page: Page, settings):
        """VLANs table should be visible."""
        page = VLANsPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        expect(admin_page.locator("table")).to_be_visible()


class TestIPManagement:
    """Tests for the IP management page."""

    def test_ip_management_page_loads(self, admin_page: Page, settings):
        """IP management page should load."""
        page = IPManagementPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_ip_pools_table_visible(self, admin_page: Page, settings):
        """IP pools table should be visible."""
        page = IPManagementPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        expect(admin_page.locator("table")).to_be_visible()


class TestFiberMap:
    """Tests for the fiber map page."""

    def test_fiber_map_page_loads(self, admin_page: Page, settings):
        """Fiber map page should load."""
        page = FiberMapPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_fiber_map_visible(self, admin_page: Page, settings):
        """Fiber map should be visible."""
        page = FiberMapPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_map_visible()


class TestNetworkAPI:
    """API-level tests for network management."""

    def test_list_olts_api(self, api_context, admin_token):
        """API should return OLTs list."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/network/olt-devices?limit=10",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200
        data = response.json()
        assert "items" in data

    def test_list_onts_api(self, api_context, admin_token):
        """API should return ONTs list."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/network/ont-devices?limit=10",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200
        data = response.json()
        assert "items" in data

    def test_list_vlans_api(self, api_context, admin_token):
        """API should return VLANs list."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/network/vlans?limit=10",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200
        data = response.json()
        assert "items" in data

    def test_list_ip_pools_api(self, api_context, admin_token):
        """API should return IP pools list."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/network/ip-pools?limit=10",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200
        data = response.json()
        assert "items" in data

    def test_list_radius_profiles_api(self, api_context, admin_token):
        """API should return RADIUS profiles list."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/catalog/radius-profiles?limit=10",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200
        data = response.json()
        assert "items" in data


class TestNetworkNavigation:
    """Tests for network navigation."""

    def test_navigate_to_network(self, admin_page: Page, settings):
        """Should navigate to network from dashboard."""
        from tests.playwright.pages.admin.dashboard_page import AdminDashboardPage

        dashboard = AdminDashboardPage(admin_page, settings.base_url)
        dashboard.goto()
        dashboard.expect_loaded()
        admin_page.get_by_role("link", name="Network").first.click()
        admin_page.wait_for_url("**/network**")

    def test_navigate_between_network_pages(self, admin_page: Page, settings):
        """Should navigate between network sub-pages."""
        olts = OLTsPage(admin_page, settings.base_url)
        olts.goto()
        olts.expect_loaded()

        # Navigate to ONTs
        admin_page.get_by_role("link", name="ONT").first.click()
        admin_page.wait_for_url("**/onts**")
