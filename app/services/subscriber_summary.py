"""Subscriber summary projection — single read-owner.

One server-owned identity+status+plan+balance+connection+address projection for a
subscriber, composed from the existing SOT reads (subscriber, financial position,
catalog subscriptions, RADIUS accounting, status presentation). Consumed by the
customer-360 page and the team-inbox context rail as thin adapters, so neither
re-derives subscriber state in a template.

Deliberately excluded (require a live device poll or the lazy usage endpoint, and
must NOT run on every render): ONT optical signal / rx_power, FUP / live data
usage. Connection status here is the cheap DB read over RADIUS accounting.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.services import catalog as catalog_service
from app.services import subscriber as subscriber_service
from app.services.customer_financial_position import get_customer_financial_position
from app.services.network.radius_sessions import (
    latest_open_accounting_sessions_by_subscription,
)
from app.services.status_presentation import (
    account_status_presentation,
    subscription_status_presentation,
)


def _tone(tone: object) -> str:
    """Semantic tone as a plain string (positive/info/warning/negative/neutral)."""
    return getattr(tone, "value", None) or str(tone or "neutral")


def _status_value(status: object) -> str:
    return getattr(status, "value", None) or str(status or "")


def _display_name(subscriber: Any) -> str | None:
    name = getattr(subscriber, "name", None)
    if name:
        return name
    parts = [
        getattr(subscriber, "display_name", None),
        getattr(subscriber, "company_name", None),
    ]
    for candidate in parts:
        if candidate:
            return candidate
    first = getattr(subscriber, "first_name", None)
    last = getattr(subscriber, "last_name", None)
    joined = " ".join(part for part in (first, last) if part)
    return joined or getattr(subscriber, "email", None)


def subscriber_summary(db: Session, subscriber_id: str | None) -> dict | None:
    """Return the summary projection for ``subscriber_id`` or ``None``.

    Never raises on partial data: a missing subscription/balance/session leaves
    that section ``None`` rather than breaking the caller's page.
    """
    if not subscriber_id:
        return None
    try:
        subscriber = subscriber_service.subscribers.get(
            db=db, subscriber_id=subscriber_id
        )
    except Exception:  # noqa: BLE001 - a bad/stale id must not break the caller
        return None
    if subscriber is None:
        return None

    account_id = getattr(subscriber, "id", None)
    kind = "business" if getattr(subscriber, "is_business", False) else "person"

    presentation = account_status_presentation(
        getattr(subscriber, "status", None),
        is_active=getattr(subscriber, "is_active", None),
    )
    status = {
        "label": presentation.label,
        "tone": _tone(presentation.tone),
        "value": presentation.value,
    }

    # Plan — the primary active subscription (there may be several).
    plan: dict | None = None
    active_subscription_ids: list = []
    try:
        subscriptions = catalog_service.subscriptions.list(
            db=db,
            subscriber_id=str(account_id),
            offer_id=None,
            status=None,
            order_by="created_at",
            order_dir="desc",
            limit=200,
            offset=0,
        )
    except Exception:  # noqa: BLE001
        subscriptions = []
    active = [
        s
        for s in subscriptions
        if _status_value(getattr(s, "status", None)) == "active"
    ]
    active_subscription_ids = [
        getattr(s, "id", None) for s in active if getattr(s, "id", None)
    ]
    if active:
        primary = active[0]
        offer = getattr(primary, "offer", None)
        sub_presentation = subscription_status_presentation(
            getattr(primary, "status", None)
        )
        plan = {
            "name": getattr(offer, "name", None) if offer else None,
            "price": getattr(primary, "unit_price", None),
            "next_billing_at": getattr(primary, "next_billing_at", None),
            "status_label": sub_presentation.label,
            "status_tone": _tone(sub_presentation.tone),
        }

    # Balance — the SOT financial position (currency-safe, per account).
    balance: dict | None = None
    if account_id is not None:
        try:
            position = get_customer_financial_position(db, account_id)
            balance = {
                "outstanding": position.open_invoice_balance,
                "overdue": position.overdue_debt_balance,
                "overdue_count": position.overdue_invoice_count,
                "prepaid": position.prepaid_available_balance,
                "currency": position.currency,
                "days_overdue": position.days_overdue,
            }
        except Exception:  # noqa: BLE001
            balance = None

    # Connection — cheap RADIUS accounting DB read (never a live device poll).
    connection: dict | None = None
    if active_subscription_ids:
        try:
            sessions = latest_open_accounting_sessions_by_subscription(
                db, active_subscription_ids
            )
        except Exception:  # noqa: BLE001
            sessions = {}
        if sessions:
            observed_at = [
                value
                for session in sessions.values()
                if isinstance(
                    (value := getattr(session, "last_update_at", None)), datetime
                )
            ]
            last_seen = max(observed_at, default=None)
            ip = next(
                (
                    getattr(s, "framed_ip_address", None)
                    for s in sessions.values()
                    if getattr(s, "framed_ip_address", None)
                ),
                None,
            )
            connection = {"online": True, "last_seen_at": last_seen, "ip": ip}
        else:
            connection = {"online": False, "last_seen_at": None, "ip": None}

    # Address — flattened service-address columns (the richer Address model with
    # lat/lng belongs to the network/map surfaces, not this summary).
    address: dict | None = None
    line1 = getattr(subscriber, "address_line1", None)
    city = getattr(subscriber, "city", None)
    region = getattr(subscriber, "region", None)
    if line1 or city or region:
        address = {"line1": line1, "city": city, "region": region}

    return {
        "id": str(subscriber_id),
        "url": f"/admin/customers/{kind}/{subscriber_id}",
        "is_business": kind == "business",
        "name": _display_name(subscriber),
        "account_number": getattr(subscriber, "account_number", None),
        "subscriber_number": getattr(subscriber, "subscriber_number", None),
        "email": getattr(subscriber, "email", None),
        "phone": getattr(subscriber, "phone", None),
        "since": getattr(subscriber, "account_start_date", None)
        or getattr(subscriber, "created_at", None),
        "status": status,
        "plan": plan,
        "active_plan_count": len(active),
        "balance": balance,
        "connection": connection,
        "address": address,
    }
