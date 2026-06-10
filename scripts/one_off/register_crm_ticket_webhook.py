"""Register Sub's inbound CRM webhook endpoint + ticket subscriptions.

Creates (idempotently, keyed by URL) a webhook endpoint in the CRM pointing
at our /api/v1/webhooks/crm receiver with the shared HMAC secret, and
subscribes it to the ticket events the CRM emits. After this, new CRM
tickets sync locally in seconds; the 5-minute incremental pull remains the
safety net and covers updates/comments (which have no CRM webhook events).

Requires CRM_WEBHOOK_SECRET in the environment (same value the receiver
verifies) and the public base URL of this app.

Usage:
    python -m scripts.one_off.register_crm_ticket_webhook \
        --app-base-url https://selfcare.example.com [--dry-run]
"""

from __future__ import annotations

import argparse

from app.config import settings
from app.services.crm_client import get_crm_client

TICKET_EVENTS = ("ticket.created", "ticket.resolved", "ticket.escalated")
ENDPOINT_NAME = "dotmac-sub-ticket-sync"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app-base-url",
        required=True,
        help="public base URL of this app (e.g. https://selfcare.example.com)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not settings.crm_webhook_secret:
        raise SystemExit("CRM_WEBHOOK_SECRET is not set; configure it first.")

    url = args.app_base_url.rstrip("/") + "/api/v1/webhooks/crm"
    client = get_crm_client()

    endpoints = client._cached_get("/api/v1/webhooks/endpoints", {"limit": 200}, 0)
    items = endpoints.get("items") if isinstance(endpoints, dict) else endpoints
    existing = next((e for e in (items or []) if e.get("url") == url), None)

    if args.dry_run:
        print(f"[dry-run] endpoint: {'exists' if existing else 'would create'} {url}")
        return

    if existing:
        endpoint_id = str(existing["id"])
        print(f"endpoint exists: {endpoint_id}")
    else:
        created = client._request(
            "POST",
            "/api/v1/webhooks/endpoints",
            json_data={
                "name": ENDPOINT_NAME,
                "url": url,
                "secret": settings.crm_webhook_secret,
                "is_active": True,
            },
        )
        endpoint_id = str(created["id"])
        print(f"endpoint created: {endpoint_id}")

    subs = client._cached_get("/api/v1/webhooks/subscriptions", {"limit": 200}, 0)
    sub_items = subs.get("items") if isinstance(subs, dict) else subs
    have = {
        str(s.get("event_type"))
        for s in (sub_items or [])
        if str(s.get("endpoint_id")) == endpoint_id
    }
    for event in TICKET_EVENTS:
        if event in have:
            print(f"subscription exists: {event}")
            continue
        client._request(
            "POST",
            "/api/v1/webhooks/subscriptions",
            json_data={
                "endpoint_id": endpoint_id,
                "event_type": event,
                "is_active": True,
            },
        )
        print(f"subscribed: {event}")


if __name__ == "__main__":
    main()
