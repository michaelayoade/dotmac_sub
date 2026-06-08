"""Reseller portal page objects."""

from __future__ import annotations

from tests.playwright.pages.reseller.account_detail_page import (
    ResellerAccountDetailPage,
)
from tests.playwright.pages.reseller.accounts_page import ResellerAccountsPage
from tests.playwright.pages.reseller.billing_page import ResellerBillingPage
from tests.playwright.pages.reseller.dashboard_page import ResellerDashboardPage
from tests.playwright.pages.reseller.fiber_map_page import ResellerFiberMapPage
from tests.playwright.pages.reseller.invoices_page import (
    ResellerAccountInvoicesPage,
    ResellerInvoiceDetailPage,
)
from tests.playwright.pages.reseller.login_page import ResellerLoginPage
from tests.playwright.pages.reseller.profile_page import (
    ResellerMfaSetupPage,
    ResellerProfilePage,
)
from tests.playwright.pages.reseller.reports_page import ResellerRevenueReportPage
from tests.playwright.pages.reseller.tickets_page import ResellerAccountTicketsPage

__all__ = [
    "ResellerLoginPage",
    "ResellerDashboardPage",
    "ResellerAccountsPage",
    "ResellerAccountDetailPage",
    "ResellerAccountInvoicesPage",
    "ResellerInvoiceDetailPage",
    "ResellerAccountTicketsPage",
    "ResellerProfilePage",
    "ResellerMfaSetupPage",
    "ResellerBillingPage",
    "ResellerRevenueReportPage",
    "ResellerFiberMapPage",
]
