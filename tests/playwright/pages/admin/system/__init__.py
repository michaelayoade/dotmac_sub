"""System page objects."""

from tests.playwright.pages.admin.system.api_keys_page import APIKeysPage
from tests.playwright.pages.admin.system.audit_page import AuditPage
from tests.playwright.pages.admin.system.roles_page import RolesPage
from tests.playwright.pages.admin.system.settings_page import SettingsPage
from tests.playwright.pages.admin.system.users_page import UsersPage
from tests.playwright.pages.admin.system.webhooks_page import WebhooksPage

__all__ = [
    "UsersPage",
    "RolesPage",
    "APIKeysPage",
    "WebhooksPage",
    "AuditPage",
    "SettingsPage",
]
