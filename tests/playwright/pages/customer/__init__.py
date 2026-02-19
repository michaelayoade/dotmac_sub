"""Customer portal page objects."""

from __future__ import annotations

from tests.playwright.pages.customer.billing_page import CustomerBillingPage
from tests.playwright.pages.customer.dashboard_page import CustomerDashboardPage
from tests.playwright.pages.customer.profile_page import CustomerProfilePage
from tests.playwright.pages.customer.services_page import CustomerServicesPage
from tests.playwright.pages.customer.support_page import CustomerSupportPage
from tests.playwright.pages.customer.ticket_page import CustomerTicketPage
from tests.playwright.pages.customer.usage_page import CustomerUsagePage

__all__ = [
    "CustomerDashboardPage",
    "CustomerBillingPage",
    "CustomerServicesPage",
    "CustomerSupportPage",
    "CustomerTicketPage",
    "CustomerUsagePage",
    "CustomerProfilePage",
]
