from __future__ import annotations

from tests.playwright.pages.admin.dashboard_page import AdminDashboardPage
from tests.playwright.pages.admin.tickets_page import AdminTicketsPage


def test_admin_dashboard_smoke(admin_page, settings):
    dashboard = AdminDashboardPage(admin_page, settings.base_url)
    dashboard.goto()
    dashboard.expect_loaded()


def test_admin_tickets_list_smoke(admin_page, settings):
    tickets = AdminTicketsPage(admin_page, settings.base_url)
    tickets.goto()
    tickets.expect_loaded()
