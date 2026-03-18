"""CRM portal service — orchestrates CRM API calls for customer/reseller portals.

Handles subscriber ID resolution, Redis caching, and template context building
for tickets and work orders sourced from the external CRM.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.services.common import coerce_uuid
from app.services.crm_client import CRMClientError, get_crm_client
from app.services.session_store import get_session_redis

logger = logging.getLogger(__name__)

# ── Cache TTLs ───────────────────────────────────────────────────────────

_CACHE_SUBSCRIBER_MAP = 3600  # 1hr — subscriber ID mapping
_CACHE_LIST = 60  # 60s — list pages
_CACHE_DETAIL = 30  # 30s — detail pages

# ── Status display dicts (no Tailwind interpolation) ─────────────────────

TICKET_STATUS_DISPLAY: dict[str, str] = {
    "open": "Open",
    "in_progress": "In Progress",
    "waiting_on_customer": "Waiting on Customer",
    "waiting_on_agent": "Waiting on Agent",
    "resolved": "Resolved",
    "closed": "Closed",
}

TICKET_STATUS_COLORS: dict[str, str] = {
    "open": "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
    "in_progress": "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200",
    "waiting_on_customer": "bg-violet-100 text-violet-800 dark:bg-violet-900 dark:text-violet-200",
    "waiting_on_agent": "bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200",
    "resolved": "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
    "closed": "bg-slate-100 text-slate-800 dark:bg-slate-700 dark:text-slate-200",
}

TICKET_PRIORITY_DISPLAY: dict[str, str] = {
    "low": "Low",
    "normal": "Normal",
    "high": "High",
    "urgent": "Urgent",
}

TICKET_PRIORITY_COLORS: dict[str, str] = {
    "low": "bg-slate-100 text-slate-700 dark:bg-slate-700 dark:text-slate-300",
    "normal": "bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300",
    "high": "bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-300",
    "urgent": "bg-rose-100 text-rose-700 dark:bg-rose-900 dark:text-rose-300",
}

WORK_ORDER_STATUS_DISPLAY: dict[str, str] = {
    "draft": "Draft",
    "scheduled": "Scheduled",
    "in_progress": "In Progress",
    "completed": "Completed",
    "cancelled": "Cancelled",
    "on_hold": "On Hold",
}

WORK_ORDER_STATUS_COLORS: dict[str, str] = {
    "draft": "bg-slate-100 text-slate-800 dark:bg-slate-700 dark:text-slate-200",
    "scheduled": "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
    "in_progress": "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200",
    "completed": "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
    "cancelled": "bg-rose-100 text-rose-800 dark:bg-rose-900 dark:text-rose-200",
    "on_hold": "bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200",
}

WORK_ORDER_TYPE_DISPLAY: dict[str, str] = {
    "installation": "Installation",
    "repair": "Repair",
    "maintenance": "Maintenance",
    "upgrade": "Upgrade",
    "relocation": "Relocation",
    "decommission": "Decommission",
}


# ── Subscriber ID Resolution ────────────────────────────────────────────

def _cache_get(key: str) -> str | None:
    """Get a value from Redis cache, or None."""
    r = get_session_redis()
    if not r:
        return None
    try:
        val = r.get(key)
        return str(val) if val is not None else None
    except Exception:
        return None


def _cache_set(key: str, value: str, ttl: int) -> None:
    """Set a value in Redis cache with TTL."""
    r = get_session_redis()
    if not r:
        return
    try:
        r.setex(key, ttl, value)
    except Exception:
        pass


def resolve_crm_subscriber_id(db: Session, subscriber_id: str) -> str | None:
    """Resolve a DotMac Sub subscriber UUID to a CRM subscriber UUID.

    Chain: Sub UUID → splynx_customer_id → CRM external_id lookup → CRM UUID.
    Cached in Redis for 1hr.
    """
    cache_key = f"crm:sub_map:{subscriber_id}"
    cached = _cache_get(cache_key)
    if cached:
        return cached if cached != "__none__" else None

    subscriber = db.get(Subscriber, coerce_uuid(subscriber_id))
    if not subscriber or not subscriber.splynx_customer_id:
        _cache_set(cache_key, "__none__", _CACHE_SUBSCRIBER_MAP)
        return None

    client = get_crm_client()
    crm_id = client.resolve_subscriber_id(subscriber.splynx_customer_id)
    if crm_id:
        _cache_set(cache_key, crm_id, _CACHE_SUBSCRIBER_MAP)
    else:
        _cache_set(cache_key, "__none__", 300)  # shorter TTL for misses
    return crm_id


def resolve_crm_subscriber_ids(db: Session, subscriber_ids: Iterable[str]) -> list[str]:
    """Resolve multiple DotMac subscriber UUIDs to CRM subscriber UUIDs."""
    resolved: list[str] = []
    seen: set[str] = set()
    for subscriber_id in subscriber_ids:
        candidate = str(subscriber_id or "").strip()
        if not candidate:
            continue
        crm_id = resolve_crm_subscriber_id(db, candidate)
        if crm_id and crm_id not in seen:
            seen.add(crm_id)
            resolved.append(crm_id)
    return resolved


def _sort_by_recent(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort CRM items by updated_at/created_at descending."""
    return sorted(
        items,
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )


def _error_context(message: str = "Unable to reach support system. Please try again later.") -> dict[str, Any]:
    """Return CRM error flags for template context."""
    return {"crm_error": True, "crm_error_message": message}


def _ok_context() -> dict[str, Any]:
    """Return clean CRM status flags."""
    return {"crm_error": False, "crm_error_message": ""}


# ── Customer Portal: Tickets ────────────────────────────────────────────

def tickets_list_context(
    request: Request,
    db: Session,
    customer: dict,
    subscriber_ids: list[str],
) -> dict[str, Any]:
    """Build template context for customer ticket list."""
    try:
        crm_sub_ids = resolve_crm_subscriber_ids(db, subscriber_ids)
        if not crm_sub_ids:
            return {
                "request": request,
                "customer": customer,
                "tickets": [],
                "active_page": "support",
                "status_display": TICKET_STATUS_DISPLAY,
                "status_colors": TICKET_STATUS_COLORS,
                "priority_display": TICKET_PRIORITY_DISPLAY,
                "priority_colors": TICKET_PRIORITY_COLORS,
                **_ok_context(),
            }
        client = get_crm_client()
        merged: dict[str, dict[str, Any]] = {}
        for crm_sub_id in crm_sub_ids:
            for ticket in client.list_tickets(subscriber_id=crm_sub_id):
                ticket_id = str(ticket.get("id") or "")
                if ticket_id:
                    merged[ticket_id] = ticket
        tickets = _sort_by_recent(list(merged.values()))
    except CRMClientError:
        return {
            "request": request,
            "customer": customer,
            "tickets": [],
            "active_page": "support",
            "status_display": TICKET_STATUS_DISPLAY,
            "status_colors": TICKET_STATUS_COLORS,
            "priority_display": TICKET_PRIORITY_DISPLAY,
            "priority_colors": TICKET_PRIORITY_COLORS,
            **_error_context(),
        }

    return {
        "request": request,
        "customer": customer,
        "tickets": tickets,
        "active_page": "support",
        "status_display": TICKET_STATUS_DISPLAY,
        "status_colors": TICKET_STATUS_COLORS,
        "priority_display": TICKET_PRIORITY_DISPLAY,
        "priority_colors": TICKET_PRIORITY_COLORS,
        **_ok_context(),
    }


def ticket_detail_context(
    request: Request,
    db: Session,
    customer: dict,
    subscriber_ids: list[str],
    ticket_id: str,
) -> dict[str, Any]:
    """Build template context for customer ticket detail."""
    try:
        crm_sub_ids = set(resolve_crm_subscriber_ids(db, subscriber_ids))
        if not crm_sub_ids:
            return {
                "request": request,
                "customer": customer,
                "ticket": None,
                "comments": [],
                "active_page": "support",
                **_error_context("Ticket not found."),
            }
        client = get_crm_client()
        ticket = client.get_ticket(ticket_id)

        # Verify ticket belongs to this subscriber
        ticket_sub = str(ticket.get("subscriber_id", ""))
        if not ticket_sub or ticket_sub not in crm_sub_ids:
            return {
                "request": request,
                "customer": customer,
                "ticket": None,
                "comments": [],
                "active_page": "support",
                **_error_context("Ticket not found."),
            }

        comments_raw = client.list_ticket_comments(ticket_id)
        # Filter out internal comments
        comments = [c for c in comments_raw if not c.get("is_internal", False)]
    except CRMClientError:
        return {
            "request": request,
            "customer": customer,
            "ticket": None,
            "comments": [],
            "active_page": "support",
            **_error_context(),
        }

    return {
        "request": request,
        "customer": customer,
        "ticket": ticket,
        "comments": comments,
        "active_page": "support",
        "status_display": TICKET_STATUS_DISPLAY,
        "status_colors": TICKET_STATUS_COLORS,
        "priority_display": TICKET_PRIORITY_DISPLAY,
        "priority_colors": TICKET_PRIORITY_COLORS,
        **_ok_context(),
    }


def ticket_create_context(
    request: Request,
    customer: dict,
) -> dict[str, Any]:
    """Build template context for ticket creation form."""
    return {
        "request": request,
        "customer": customer,
        "active_page": "support",
        "priorities": list(TICKET_PRIORITY_DISPLAY.keys()),
        "priority_display": TICKET_PRIORITY_DISPLAY,
        **_ok_context(),
    }


def handle_ticket_create(
    db: Session,
    customer: dict,
    subscriber_id: str,
    title: str,
    description: str,
    priority: str,
) -> dict[str, Any]:
    """Create a ticket in the CRM.

    Returns:
        Dict with 'success' bool and 'ticket' or 'error' key.
    """
    try:
        crm_sub_id = resolve_crm_subscriber_id(db, subscriber_id)
        if not crm_sub_id:
            return {"success": False, "error": "Unable to link your account to the support system."}

        client = get_crm_client()
        ticket = client.create_ticket({
            "subscriber_id": crm_sub_id,
            "title": title,
            "description": description or "",
            "priority": priority if priority in TICKET_PRIORITY_DISPLAY else "normal",
            "source": "customer_portal",
        })
        return {"success": True, "ticket": ticket}
    except CRMClientError as e:
        logger.error("Failed to create CRM ticket: %s", e)
        return {"success": False, "error": "Unable to create ticket. Please try again later."}


def handle_ticket_comment(
    db: Session,
    customer: dict,
    subscriber_ids: list[str],
    ticket_id: str,
    body: str,
) -> dict[str, Any]:
    """Add a comment to a CRM ticket.

    Returns:
        Dict with 'success' bool.
    """
    try:
        crm_sub_ids = set(resolve_crm_subscriber_ids(db, subscriber_ids))
        if not crm_sub_ids:
            return {"success": False, "error": "Ticket not found."}
        client = get_crm_client()
        ticket = client.get_ticket(ticket_id)
        if str(ticket.get("subscriber_id", "")) not in crm_sub_ids:
            return {"success": False, "error": "Ticket not found."}
        client.create_ticket_comment({
            "ticket_id": ticket_id,
            "body": body,
            "is_internal": False,
            "author_name": customer.get("current_user", {}).get("name", "Customer"),
        })
        return {"success": True}
    except CRMClientError as e:
        logger.error("Failed to create CRM ticket comment: %s", e)
        return {"success": False, "error": "Unable to add comment. Please try again later."}


# ── Customer Portal: Work Orders ─────────────────────────────────────────

def work_orders_list_context(
    request: Request,
    db: Session,
    customer: dict,
    subscriber_ids: list[str],
) -> dict[str, Any]:
    """Build template context for customer work order list."""
    try:
        crm_sub_ids = resolve_crm_subscriber_ids(db, subscriber_ids)
        if not crm_sub_ids:
            return {
                "request": request,
                "customer": customer,
                "work_orders": [],
                "active_page": "work-orders",
                "status_display": WORK_ORDER_STATUS_DISPLAY,
                "status_colors": WORK_ORDER_STATUS_COLORS,
                "type_display": WORK_ORDER_TYPE_DISPLAY,
                **_ok_context(),
            }
        client = get_crm_client()
        merged: dict[str, dict[str, Any]] = {}
        for crm_sub_id in crm_sub_ids:
            for work_order in client.list_work_orders(subscriber_id=crm_sub_id):
                work_order_id = str(work_order.get("id") or "")
                if work_order_id:
                    merged[work_order_id] = work_order
        work_orders = _sort_by_recent(list(merged.values()))
    except CRMClientError:
        return {
            "request": request,
            "customer": customer,
            "work_orders": [],
            "active_page": "work-orders",
            "status_display": WORK_ORDER_STATUS_DISPLAY,
            "status_colors": WORK_ORDER_STATUS_COLORS,
            "type_display": WORK_ORDER_TYPE_DISPLAY,
            **_error_context(),
        }

    return {
        "request": request,
        "customer": customer,
        "work_orders": work_orders,
        "active_page": "work-orders",
        "status_display": WORK_ORDER_STATUS_DISPLAY,
        "status_colors": WORK_ORDER_STATUS_COLORS,
        "type_display": WORK_ORDER_TYPE_DISPLAY,
        **_ok_context(),
    }


def work_order_detail_context(
    request: Request,
    db: Session,
    customer: dict,
    subscriber_ids: list[str],
    work_order_id: str,
) -> dict[str, Any]:
    """Build template context for customer work order detail."""
    try:
        crm_sub_ids = set(resolve_crm_subscriber_ids(db, subscriber_ids))
        if not crm_sub_ids:
            return {
                "request": request,
                "customer": customer,
                "work_order": None,
                "notes": [],
                "active_page": "work-orders",
                **_error_context("Work order not found."),
            }
        client = get_crm_client()
        work_order = client.get_work_order(work_order_id)

        # Verify work order belongs to this subscriber
        wo_sub = str(work_order.get("subscriber_id", ""))
        if not wo_sub or wo_sub not in crm_sub_ids:
            return {
                "request": request,
                "customer": customer,
                "work_order": None,
                "notes": [],
                "active_page": "work-orders",
                **_error_context("Work order not found."),
            }

        notes = client.list_work_order_notes(work_order_id)
    except CRMClientError:
        return {
            "request": request,
            "customer": customer,
            "work_order": None,
            "notes": [],
            "active_page": "work-orders",
            **_error_context(),
        }

    return {
        "request": request,
        "customer": customer,
        "work_order": work_order,
        "notes": notes,
        "active_page": "work-orders",
        "status_display": WORK_ORDER_STATUS_DISPLAY,
        "status_colors": WORK_ORDER_STATUS_COLORS,
        "type_display": WORK_ORDER_TYPE_DISPLAY,
        **_ok_context(),
    }


# ── Reseller Portal ─────────────────────────────────────────────────────

def reseller_account_tickets_context(
    request: Request,
    db: Session,
    account_id: str,
    current_user: dict,
    reseller: Any,
) -> dict[str, Any]:
    """Build template context for reseller viewing a customer's tickets."""
    try:
        crm_sub_id = resolve_crm_subscriber_id(db, account_id)
        if not crm_sub_id:
            return {
                "request": request,
                "current_user": current_user,
                "reseller": reseller,
                "tickets": [],
                "account_id": account_id,
                "active_page": "accounts",
                "status_display": TICKET_STATUS_DISPLAY,
                "status_colors": TICKET_STATUS_COLORS,
                **_ok_context(),
            }
        client = get_crm_client()
        tickets = client.list_tickets(subscriber_id=crm_sub_id)
    except CRMClientError:
        return {
            "request": request,
            "current_user": current_user,
            "reseller": reseller,
            "tickets": [],
            "account_id": account_id,
            "active_page": "accounts",
            "status_display": TICKET_STATUS_DISPLAY,
            "status_colors": TICKET_STATUS_COLORS,
            **_error_context(),
        }

    return {
        "request": request,
        "current_user": current_user,
        "reseller": reseller,
        "tickets": tickets,
        "account_id": account_id,
        "active_page": "accounts",
        "status_display": TICKET_STATUS_DISPLAY,
        "status_colors": TICKET_STATUS_COLORS,
        **_ok_context(),
    }


def reseller_open_tickets_count(
    db: Session,
    reseller_id: str,
    account_ids: list[str],
) -> int:
    """Count open tickets across all reseller accounts.

    Fails silently (returns 0) if CRM is unreachable.
    """
    total = 0
    client = get_crm_client()
    for account_id in account_ids:
        try:
            crm_sub_id = resolve_crm_subscriber_id(db, account_id)
            if not crm_sub_id:
                continue
            tickets = client.list_tickets(subscriber_id=crm_sub_id)
            total += sum(
                1 for t in tickets
                if t.get("status") in ("open", "in_progress", "waiting_on_agent")
            )
        except CRMClientError:
            logger.debug("CRM unreachable for open ticket count, skipping")
            return 0
    return total
