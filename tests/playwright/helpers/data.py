from __future__ import annotations

from typing import Any

from tests.playwright.helpers.api import api_get, api_post_json, bearer_headers
from tests.playwright.helpers.auth import AuthError, ensure_person


def ensure_person_subscriber_account(
    api_context,
    token: str,
    first_name: str,
    last_name: str,
    email: str,
) -> dict[str, Any]:
    headers = bearer_headers(token)
    person = ensure_person(api_context, token, first_name, last_name, email)

    response = api_get(
        api_context,
        f"/api/v1/subscribers?subscriber_type=person&person_id={person['id']}",
        headers=headers,
    )
    if not response.ok:
        raise AuthError(f"Failed to list subscribers: {response.status}")
    data = response.json()
    subscribers = data.get("items", [])
    if subscribers:
        subscriber = subscribers[0]
    else:
        response = api_post_json(
            api_context,
            "/api/v1/subscribers",
            {"person_id": person["id"]},
            headers=headers,
        )
        if not response.ok:
            raise AuthError(f"Failed to create subscriber: {response.status}")
        subscriber = response.json()

    response = api_get(
        api_context,
        f"/api/v1/subscriber-accounts?subscriber_id={subscriber['id']}",
        headers=headers,
    )
    if not response.ok:
        raise AuthError(f"Failed to list accounts: {response.status}")
    data = response.json()
    accounts = data.get("items", [])
    if accounts:
        account = accounts[0]
    else:
        response = api_post_json(
            api_context,
            "/api/v1/subscriber-accounts",
            {"subscriber_id": subscriber["id"]},
            headers=headers,
        )
        if not response.ok:
            raise AuthError(f"Failed to create account: {response.status}")
        account = response.json()

    return {
        "person": person,
        "subscriber": subscriber,
        "account": account,
    }


def create_test_invoice(
    api_context,
    token: str,
    account_id: str,
    status: str = "draft",
    total_amount: float = 100.00,
) -> dict[str, Any]:
    """Create a test invoice via API."""
    headers = bearer_headers(token)
    response = api_post_json(
        api_context,
        "/api/v1/billing/invoices",
        {
            "account_id": account_id,
            "status": status,
            "currency": "USD",
            "total_amount": total_amount,
        },
        headers=headers,
    )
    if not response.ok:
        raise AuthError(f"Failed to create invoice: {response.status}")
    return response.json()


def create_test_payment(
    api_context,
    token: str,
    account_id: str,
    amount: float,
    payment_method: str = "credit_card",
) -> dict[str, Any]:
    """Create a test payment via API."""
    headers = bearer_headers(token)
    response = api_post_json(
        api_context,
        "/api/v1/billing/payments",
        {
            "account_id": account_id,
            "amount": amount,
            "payment_method": payment_method,
            "status": "completed",
            "currency": "USD",
        },
        headers=headers,
    )
    if not response.ok:
        raise AuthError(f"Failed to create payment: {response.status}")
    return response.json()


def create_test_ledger_entry(
    api_context,
    token: str,
    account_id: str,
    entry_type: str,
    amount: float,
    description: str = "Test entry",
) -> dict[str, Any]:
    """Create a test ledger entry via API."""
    headers = bearer_headers(token)
    response = api_post_json(
        api_context,
        "/api/v1/billing/ledger",
        {
            "account_id": account_id,
            "entry_type": entry_type,
            "amount": amount,
            "description": description,
            "currency": "USD",
        },
        headers=headers,
    )
    if not response.ok:
        raise AuthError(f"Failed to create ledger entry: {response.status}")
    return response.json()


def create_test_service_order(
    api_context,
    token: str,
    account_id: str,
    status: str = "draft",
    notes: str | None = None,
) -> dict[str, Any]:
    """Create a test service order via API."""
    headers = bearer_headers(token)
    response = api_post_json(
        api_context,
        "/api/v1/provisioning/service-orders",
        {
            "account_id": account_id,
            "status": status,
            "notes": notes,
        },
        headers=headers,
    )
    if not response.ok:
        raise AuthError(f"Failed to create service order: {response.status}")
    return response.json()


def create_test_work_order(
    api_context,
    token: str,
    account_id: str,
    work_type: str = "installation",
    status: str = "pending",
) -> dict[str, Any]:
    """Create a test work order via API."""
    headers = bearer_headers(token)
    response = api_post_json(
        api_context,
        "/api/v1/workforce/work-orders",
        {
            "account_id": account_id,
            "work_type": work_type,
            "status": status,
        },
        headers=headers,
    )
    if not response.ok:
        raise AuthError(f"Failed to create work order: {response.status}")
    return response.json()
