"""Dashboard e2e tests."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.playwright.pages.admin.dashboard_page import AdminDashboardPage


class TestAdminDashboard:
    """Tests for admin dashboard functionality."""

    def test_dashboard_loads(self, admin_page: Page, settings):
        """Dashboard should load with Operations Center heading."""
        dashboard = AdminDashboardPage(admin_page, settings.base_url)
        dashboard.goto()
        dashboard.expect_loaded()

    def test_dashboard_shows_stats(self, admin_page: Page, settings):
        """Dashboard should display stats cards."""
        dashboard = AdminDashboardPage(admin_page, settings.base_url)
        dashboard.goto()
        dashboard.expect_loaded()
        dashboard.expect_stats_visible()

    def test_dashboard_shows_activity(self, admin_page: Page, settings):
        """Dashboard should display recent activity feed."""
        dashboard = AdminDashboardPage(admin_page, settings.base_url)
        dashboard.goto()
        dashboard.expect_loaded()
        dashboard.expect_activity_feed_visible()

    def test_dashboard_sidebar_navigation(self, admin_page: Page, settings):
        """Sidebar should be visible with navigation links."""
        dashboard = AdminDashboardPage(admin_page, settings.base_url)
        dashboard.goto()
        dashboard.expect_loaded()
        dashboard.expect_sidebar_visible()

    def test_navigate_to_subscribers(self, admin_page: Page, settings):
        """Clicking subscribers link should navigate to subscribers page."""
        dashboard = AdminDashboardPage(admin_page, settings.base_url)
        dashboard.goto()
        dashboard.expect_loaded()
        dashboard.click_subscribers_link()
        admin_page.wait_for_url("**/admin/subscribers**")
        expect(admin_page.get_by_role("heading", name="Subscribers")).to_be_visible()

    def test_navigate_to_tickets(self, admin_page: Page, settings):
        """Clicking tickets link should navigate to tickets page."""
        dashboard = AdminDashboardPage(admin_page, settings.base_url)
        dashboard.goto()
        dashboard.expect_loaded()
        dashboard.click_tickets_link()
        admin_page.wait_for_url("**/admin/tickets**")
        expect(admin_page.get_by_role("heading", name="Tickets")).to_be_visible()
