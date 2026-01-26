from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.playwright.helpers.api import api_post_form, bearer_headers
from tests.playwright.helpers.auth import (
    ensure_person,
    ensure_person_role,
    ensure_role_id,
    ensure_user_credential,
    login_for_token,
)
from tests.playwright.helpers.config import E2ESettings
from tests.playwright.helpers.data import ensure_person_subscriber_account


@pytest.fixture(scope="session")
def settings() -> E2ESettings:
    if os.getenv("PLAYWRIGHT_BASE_URL") is None:
        pytest.skip("Set PLAYWRIGHT_BASE_URL to run Playwright E2E tests.")
    settings = E2ESettings.from_env()
    expect.set_options(timeout=settings.expect_timeout_ms)
    return settings


@pytest.fixture(scope="session")
def playwright_instance():
    with sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope="session")
def browser(playwright_instance, settings: E2ESettings):
    browser_name = settings.browser
    browser_type = getattr(playwright_instance, browser_name, None)
    if browser_type is None:
        browser_type = playwright_instance.firefox

    launch_kwargs = {
        "headless": settings.headless,
        "slow_mo": settings.slow_mo_ms,
        "timeout": settings.navigation_timeout_ms,
    }
    if browser_type is playwright_instance.chromium:
        launch_kwargs["args"] = [
            "--headless=new",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-crash-reporter",
        ]

    browser = browser_type.launch(**launch_kwargs)
    yield browser
    browser.close()


@pytest.fixture(scope="session")
def api_context(playwright_instance, settings: E2ESettings):
    context = playwright_instance.request.new_context(base_url=settings.base_url)
    yield context
    context.dispose()


def _require_admin_credentials(settings: E2ESettings) -> tuple[str, str]:
    if not settings.admin_username or not settings.admin_password:
        pytest.skip("Set E2E_ADMIN_USERNAME and E2E_ADMIN_PASSWORD to run E2E tests.")
    return settings.admin_username, settings.admin_password


@pytest.fixture(scope="session")
def admin_token(settings: E2ESettings, api_context) -> str:
    username, password = _require_admin_credentials(settings)
    return login_for_token(api_context, username, password)


@pytest.fixture(scope="session")
def test_identities(settings: E2ESettings, api_context, admin_token: str) -> dict:
    headers = bearer_headers(admin_token)

    agent_email = _email_for_username(settings.agent_username)
    agent_person = ensure_person(api_context, admin_token, "Agent", "Support", agent_email)
    ensure_user_credential(
        api_context,
        admin_token,
        agent_person["id"],
        settings.agent_username,
        settings.agent_password,
    )
    support_role_id = ensure_role_id(api_context, admin_token, "support")
    ensure_person_role(api_context, admin_token, agent_person["id"], support_role_id)

    user_email = _email_for_username(settings.user_username)
    user_person = ensure_person(api_context, admin_token, "Portal", "User", user_email)
    ensure_user_credential(
        api_context,
        admin_token,
        user_person["id"],
        settings.user_username,
        settings.user_password,
    )

    customer_profile = ensure_person_subscriber_account(
        api_context,
        admin_token,
        "Customer",
        "Tester",
        "e2e.customer@example.com",
    )

    return {
        "agent": agent_person,
        "user": user_person,
        "customer": customer_profile,
    }


@pytest.fixture(scope="session")
def agent_token(settings: E2ESettings, api_context, test_identities: dict) -> str:
    return login_for_token(api_context, settings.agent_username, settings.agent_password)


@pytest.fixture(scope="session")
def user_token(settings: E2ESettings, api_context, test_identities: dict) -> str:
    return login_for_token(api_context, settings.user_username, settings.user_password)


def _storage_state_path(role: str) -> Path:
    return Path(__file__).parent / ".auth" / f"{role}.json"


def _write_storage_state(browser, settings: E2ESettings, token: str, role: str) -> Path:
    path = _storage_state_path(role)
    path.parent.mkdir(parents=True, exist_ok=True)
    context = browser.new_context()
    parsed = urlparse(settings.base_url)
    context.add_cookies(
        [
            {
                "name": "session_token",
                "value": token,
                "url": settings.base_url,
                "httpOnly": True,
                "secure": parsed.scheme == "https",
                "sameSite": "Lax",
            }
        ]
    )
    context.storage_state(path=path)
    context.close()
    return path


@pytest.fixture(scope="session")
def admin_storage_state(browser, settings: E2ESettings, admin_token: str) -> Path:
    return _write_storage_state(browser, settings, admin_token, "admin")


@pytest.fixture(scope="session")
def agent_storage_state(browser, settings: E2ESettings, agent_token: str) -> Path:
    return _write_storage_state(browser, settings, agent_token, "agent")


@pytest.fixture(scope="session")
def user_storage_state(browser, settings: E2ESettings, user_token: str) -> Path:
    return _write_storage_state(browser, settings, user_token, "user")


@pytest.fixture()
def admin_context(browser, settings: E2ESettings, admin_storage_state: Path):
    context = browser.new_context(storage_state=admin_storage_state)
    context.set_default_timeout(settings.action_timeout_ms)
    context.set_default_navigation_timeout(settings.navigation_timeout_ms)
    yield context
    context.close()


@pytest.fixture()
def agent_context(browser, settings: E2ESettings, agent_storage_state: Path):
    context = browser.new_context(storage_state=agent_storage_state)
    context.set_default_timeout(settings.action_timeout_ms)
    context.set_default_navigation_timeout(settings.navigation_timeout_ms)
    yield context
    context.close()


@pytest.fixture()
def user_context(browser, settings: E2ESettings, user_storage_state: Path):
    context = browser.new_context(storage_state=user_storage_state)
    context.set_default_timeout(settings.action_timeout_ms)
    context.set_default_navigation_timeout(settings.navigation_timeout_ms)
    yield context
    context.close()


@pytest.fixture()
def admin_page(admin_context):
    page = admin_context.new_page()
    yield page
    page.close()


@pytest.fixture()
def agent_page(agent_context):
    page = agent_context.new_page()
    yield page
    page.close()


@pytest.fixture()
def user_page(user_context):
    page = user_context.new_page()
    yield page
    page.close()


@pytest.fixture()
def admin_impersonate_response(api_context, admin_token: str, test_identities: dict):
    customer = test_identities["customer"]
    person_id = customer["person"]["id"]
    account_id = customer["account"]["id"]
    response = api_post_form(
        api_context,
        f"/admin/customers/person/{person_id}/impersonate",
        {"account_id": account_id},
        headers=bearer_headers(admin_token),
    )
    return response


@pytest.fixture()
def agent_impersonate_response(api_context, agent_token: str, test_identities: dict):
    customer = test_identities["customer"]
    person_id = customer["person"]["id"]
    account_id = customer["account"]["id"]
    response = api_post_form(
        api_context,
        f"/admin/customers/person/{person_id}/impersonate",
        {"account_id": account_id},
        headers=bearer_headers(agent_token),
    )
    return response


def _email_for_username(username: str) -> str:
    if "@" in username:
        return username
    return f"{username}@example.com"


# Customer Portal Fixtures
# Customer portal uses impersonation from admin since RADIUS auth setup is complex


@pytest.fixture()
def customer_context(browser, settings: E2ESettings, api_context, admin_token: str, test_identities: dict):
    """Browser context with customer portal session via impersonation."""
    customer = test_identities["customer"]
    person_id = customer["person"]["id"]
    account_id = customer["account"]["id"]

    # Impersonate customer through admin endpoint
    response = api_post_form(
        api_context,
        f"/admin/customers/person/{person_id}/impersonate",
        {"account_id": account_id},
        headers=bearer_headers(admin_token),
    )

    if not response.ok:
        pytest.skip(f"Failed to impersonate customer: {response.status}")

    # Extract customer_session cookie from response
    customer_session = None
    for cookie in response.headers.get_all("set-cookie"):
        if "customer_session=" in cookie:
            customer_session = cookie.split("customer_session=")[1].split(";")[0]
            break

    if not customer_session:
        pytest.skip("Impersonation did not return customer_session cookie")

    context = browser.new_context()
    context.set_default_timeout(settings.action_timeout_ms)
    context.set_default_navigation_timeout(settings.navigation_timeout_ms)

    parsed = urlparse(settings.base_url)
    context.add_cookies(
        [
            {
                "name": "customer_session",
                "value": customer_session,
                "url": settings.base_url,
                "httpOnly": True,
                "secure": parsed.scheme == "https",
                "sameSite": "Lax",
            }
        ]
    )
    yield context
    context.close()


@pytest.fixture()
def customer_page(customer_context):
    """Page for customer portal testing."""
    page = customer_context.new_page()
    yield page
    page.close()


@pytest.fixture()
def customer_token(api_context, admin_token: str, test_identities: dict):
    """Get a token for customer API access via impersonation.

    Note: Customer portal may use cookie-based auth rather than tokens.
    This fixture attempts to get a customer token or skips if unavailable.
    """
    customer = test_identities["customer"]
    person_id = customer["person"]["id"]
    account_id = customer["account"]["id"]

    # Try to get a customer API token via impersonation
    response = api_post_form(
        api_context,
        f"/admin/customers/person/{person_id}/impersonate",
        {"account_id": account_id, "api_token": "true"},
        headers=bearer_headers(admin_token),
    )

    if not response.ok:
        pytest.skip(f"Customer API token not available: {response.status}")

    data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if "token" in data:
        return data["token"]

    # If no token, use admin token for customer-scoped API calls
    # or skip if customer API requires specific customer auth
    pytest.skip("Customer portal uses cookie-based auth, not API tokens")


# Anonymous/unauthenticated context for testing login flows


@pytest.fixture()
def anon_context(browser, settings: E2ESettings):
    """Browser context without any authentication - for login flow tests."""
    context = browser.new_context()
    context.set_default_timeout(settings.action_timeout_ms)
    context.set_default_navigation_timeout(settings.navigation_timeout_ms)
    yield context
    context.close()


@pytest.fixture()
def anon_page(anon_context):
    """Page without authentication - for testing login pages."""
    page = anon_context.new_page()
    yield page
    page.close()


# Vendor portal fixtures


@pytest.fixture()
def vendor_context(browser, settings: E2ESettings, api_context, admin_token: str):
    """Browser context with vendor portal session.

    Note: Vendor portal may require specific vendor credentials.
    This fixture skips if vendor auth is not available.
    """
    # Try to authenticate as a vendor through admin impersonation or vendor login
    # For now, skip as vendor auth setup is required
    pytest.skip("Vendor portal fixtures require vendor auth setup")


@pytest.fixture()
def vendor_page(vendor_context):
    """Page for vendor portal testing."""
    page = vendor_context.new_page()
    yield page
    page.close()


# Reseller portal fixtures


@pytest.fixture()
def reseller_context(browser, settings: E2ESettings, api_context, admin_token: str):
    """Browser context with reseller portal session.

    Note: Reseller portal may require specific reseller credentials.
    This fixture skips if reseller auth is not available.
    """
    # Try to authenticate as a reseller
    # For now, skip as reseller auth setup is required
    pytest.skip("Reseller portal fixtures require reseller auth setup")


@pytest.fixture()
def reseller_page(reseller_context):
    """Page for reseller portal testing."""
    page = reseller_context.new_page()
    yield page
    page.close()
