"""Admin page objects."""

from tests.playwright.pages.admin.dashboard_page import AdminDashboardPage
from tests.playwright.pages.admin.subscribers_page import SubscribersPage
from tests.playwright.pages.admin.subscriber_form_page import SubscriberFormPage
from tests.playwright.pages.admin.subscriber_detail_page import SubscriberDetailPage

__all__ = [
    "AdminDashboardPage",
    "SubscribersPage",
    "SubscriberFormPage",
    "SubscriberDetailPage",
]
