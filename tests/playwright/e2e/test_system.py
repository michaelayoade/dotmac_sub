"""System administration e2e tests."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.playwright.pages.admin.system import (
    APIKeysPage,
    AuditPage,
    RolesPage,
    SettingsPage,
    UsersPage,
    WebhooksPage,
)


class TestUsers:
    """Tests for the users management page."""

    def test_users_page_loads(self, admin_page: Page, settings):
        """Users page should load."""
        page = UsersPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_users_table_visible(self, admin_page: Page, settings):
        """Users table should be visible."""
        page = UsersPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        expect(admin_page.locator("table")).to_be_visible()


class TestRoles:
    """Tests for the roles management page."""

    def test_roles_page_loads(self, admin_page: Page, settings):
        """Roles page should load."""
        page = RolesPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_roles_table_visible(self, admin_page: Page, settings):
        """Roles table should be visible."""
        page = RolesPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        expect(admin_page.locator("table")).to_be_visible()

    def test_admin_role_exists(self, admin_page: Page, settings):
        """Admin role should exist in the list."""
        page = RolesPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        page.expect_role_in_list("admin")


class TestAPIKeys:
    """Tests for the API keys management page."""

    def test_api_keys_page_loads(self, admin_page: Page, settings):
        """API keys page should load."""
        page = APIKeysPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_api_keys_table_visible(self, admin_page: Page, settings):
        """API keys table should be visible."""
        page = APIKeysPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        expect(admin_page.locator("table")).to_be_visible()


class TestWebhooks:
    """Tests for the webhooks management page."""

    def test_webhooks_page_loads(self, admin_page: Page, settings):
        """Webhooks page should load."""
        page = WebhooksPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_webhooks_table_visible(self, admin_page: Page, settings):
        """Webhooks table should be visible."""
        page = WebhooksPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        expect(admin_page.locator("table")).to_be_visible()


class TestAudit:
    """Tests for the audit log page."""

    def test_audit_page_loads(self, admin_page: Page, settings):
        """Audit page should load."""
        page = AuditPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()

    def test_audit_table_visible(self, admin_page: Page, settings):
        """Audit log table should be visible."""
        page = AuditPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()
        expect(admin_page.locator("table")).to_be_visible()


class TestSettings:
    """Tests for the settings page."""

    def test_settings_page_loads(self, admin_page: Page, settings):
        """Settings page should load."""
        page = SettingsPage(admin_page, settings.base_url)
        page.goto()
        page.expect_loaded()


class TestSystemAPI:
    """API-level tests for system administration."""

    def test_list_roles_api(self, api_context, admin_token):
        """API should return roles list."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/rbac/roles?limit=10",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200
        data = response.json()
        assert "items" in data

    def test_list_people_api(self, api_context, admin_token):
        """API should return people list."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/people?limit=10",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200
        data = response.json()
        assert "items" in data

    def test_list_audit_events_api(self, api_context, admin_token):
        """API should return audit events list."""
        from tests.playwright.helpers.api import api_get, bearer_headers

        response = api_get(
            api_context,
            "/api/v1/audit/events?limit=10",
            headers=bearer_headers(admin_token),
        )
        assert response.status == 200
        data = response.json()
        assert "items" in data


class TestSystemNavigation:
    """Tests for system navigation."""

    def test_navigate_to_system(self, admin_page: Page, settings):
        """Should navigate to system from dashboard."""
        from tests.playwright.pages.admin.dashboard_page import AdminDashboardPage

        dashboard = AdminDashboardPage(admin_page, settings.base_url)
        dashboard.goto()
        dashboard.expect_loaded()
        admin_page.get_by_role("link", name="System").first.click()
        admin_page.wait_for_url("**/system**")
