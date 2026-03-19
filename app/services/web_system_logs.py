"""Service helpers for all log viewer pages (Log Center)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.services.common import parse_date_filter as _parse_date

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PER_PAGE_DEFAULT = 50


def _parse_pagination(request: Request) -> tuple[int, int]:
    """Return (page, per_page) from request query params."""
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = min(200, max(10, int(request.query_params.get("per_page", str(_PER_PAGE_DEFAULT)))))
    except (ValueError, TypeError):
        per_page = _PER_PAGE_DEFAULT
    return page, per_page


def _build_filter_query(request: Request, exclude: set[str] | None = None) -> str:
    """Reconstruct filter query string (excluding page)."""
    exclude = (exclude or set()) | {"page"}
    parts = []
    for k, v in request.query_params.items():
        if k not in exclude and v:
            parts.append(f"{k}={v}")
    return "&".join(parts)


def _base_audit_query(
    db: Session,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    actor_type_filter: str | None = None,
    entity_type_filter: str | None = None,
    action_filter: str | None = None,
    search: str | None = None,
    page: int = 1,
    per_page: int = _PER_PAGE_DEFAULT,
) -> tuple[list[AuditEvent], int]:
    """Run a filtered, paginated AuditEvent query."""
    stmt = select(AuditEvent)

    if date_from:
        stmt = stmt.where(AuditEvent.occurred_at >= date_from)
    if date_to:
        end = date_to + timedelta(days=1)
        stmt = stmt.where(AuditEvent.occurred_at < end)
    if actor_type_filter:
        stmt = stmt.where(AuditEvent.actor_type == actor_type_filter)
    if entity_type_filter:
        stmt = stmt.where(AuditEvent.entity_type.ilike(f"%{entity_type_filter}%"))
    if action_filter:
        stmt = stmt.where(AuditEvent.action.ilike(f"%{action_filter}%"))
    if search:
        stmt = stmt.where(
            AuditEvent.action.ilike(f"%{search}%")
            | AuditEvent.entity_type.ilike(f"%{search}%")
        )

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = db.scalar(count_stmt) or 0

    stmt = stmt.order_by(AuditEvent.occurred_at.desc())
    stmt = stmt.offset((page - 1) * per_page).limit(per_page)
    entries = list(db.scalars(stmt).all())
    return entries, total


# ---------------------------------------------------------------------------
# Log Center Index
# ---------------------------------------------------------------------------

LOG_CATEGORIES: list[dict] = [
    {"name": "Operations Log", "url": "/admin/system/logs/operations", "icon": "M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2", "description": "Admin actions and operations", "color": "slate"},
    {"name": "API Log", "url": "/admin/system/logs/api", "icon": "M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4", "description": "API request audit trail", "color": "violet"},
    {"name": "Internal Log", "url": "/admin/system/logs/internal", "icon": "M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2", "description": "Automated system operations", "color": "blue"},
    {"name": "Portal Activity", "url": "/admin/system/logs/portal", "icon": "M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z", "description": "Customer portal logins and actions", "color": "indigo"},
    {"name": "Email Log", "url": "/admin/system/logs/email", "icon": "M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z", "description": "Outbound email delivery status", "color": "emerald"},
    {"name": "SMS Log", "url": "/admin/system/logs/sms", "icon": "M12 18h.01M8 21h8a2 2 0 002-2V5a2 2 0 00-2-2H8a2 2 0 00-2 2v14a2 2 0 002 2z", "description": "SMS delivery and errors", "color": "cyan"},
    {"name": "Status Changes", "url": "/admin/system/logs/status-changes", "icon": "M8 7h12m0 0l-4-4m4 4l-4 4m0 6H4m0 0l4 4m-4-4l4-4", "description": "Subscriber status transitions", "color": "amber"},
    {"name": "Service Changes", "url": "/admin/system/logs/service-changes", "icon": "M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15", "description": "Subscription and plan changes", "color": "amber"},
    {"name": "Accounting Sync", "url": "/admin/system/logs/accounting", "icon": "M9 7h6m0 10v-3m-3 3h.01M9 17h.01M9 14h.01M12 14h.01M15 11h.01M12 11h.01M9 11h.01M7 21h10a2 2 0 002-2V5a2 2 0 00-2-2H7a2 2 0 00-2 2v14a2 2 0 002 2z", "description": "Accounting software sync status", "color": "emerald"},
    {"name": "Payment Gateway", "url": "/admin/system/logs/payment-gateway", "icon": "M3 10h18M7 15h1m4 0h1m-7 4h12a3 3 0 003-3V8a3 3 0 00-3-3H6a3 3 0 00-3 3v8a3 3 0 003 3z", "description": "Payment provider transactions", "color": "emerald"},
]


def build_logs_index_context(db: Session) -> dict:
    """Return the Log Center index page context."""
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    today_count = db.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.occurred_at >= today_start)
    ) or 0

    total_count = db.scalar(select(func.count()).select_from(AuditEvent)) or 0

    return {
        "categories": LOG_CATEGORIES,
        "today_count": today_count,
        "total_count": total_count,
    }


# ---------------------------------------------------------------------------
# Generic log page builder
# ---------------------------------------------------------------------------

def _build_log_page_context(
    request: Request,
    db: Session,
    *,
    page_title: str,
    page_subtitle: str,
    entity_type_filter: str | None = None,
    actor_type_filter: str | None = None,
    action_filter_key: str = "action",
    extra_filters: dict[str, str] | None = None,
) -> dict:
    """Build a standard log page context with pagination and filtering."""
    page, per_page = _parse_pagination(request)
    date_from = _parse_date(request.query_params.get("date_from"))
    date_to = _parse_date(request.query_params.get("date_to"))
    action = request.query_params.get(action_filter_key) or None
    search = request.query_params.get("search") or None

    entries, total = _base_audit_query(
        db,
        date_from=date_from,
        date_to=date_to,
        entity_type_filter=entity_type_filter,
        actor_type_filter=actor_type_filter,
        action_filter=action,
        search=search,
        page=page,
        per_page=per_page,
    )
    context: dict = {
        "page_title": page_title,
        "page_subtitle": page_subtitle,
        "entries": entries,
        "total_count": total,
        "page": page,
        "per_page": per_page,
        "filter_query": _build_filter_query(request),
        "date_from": request.query_params.get("date_from", ""),
        "date_to": request.query_params.get("date_to", ""),
        action_filter_key: action or "",
        "search": search or "",
    }
    if extra_filters:
        context.update(extra_filters)
    return context


# ---------------------------------------------------------------------------
# Individual Log Page Builders
# ---------------------------------------------------------------------------

def build_api_logs_context(request: Request, db: Session) -> dict:
    """API request audit log."""
    return _build_log_page_context(
        request, db,
        page_title="API Request Log",
        page_subtitle="Audit trail of all API interactions",
        entity_type_filter="api",
    )


def build_operations_log_context(request: Request, db: Session) -> dict:
    """Admin operations audit log."""
    return _build_log_page_context(
        request, db,
        page_title="Operations Log",
        page_subtitle="All administrator operations including view, edit, create, and delete actions",
    )


def build_internal_log_context(request: Request, db: Session) -> dict:
    """Internal/system automated operations log."""
    return _build_log_page_context(
        request, db,
        page_title="Internal System Log",
        page_subtitle="Automated batch processing and system-triggered events",
        actor_type_filter="system",
    )


def build_portal_activity_context(request: Request, db: Session) -> dict:
    """Customer portal activity log."""
    return _build_log_page_context(
        request, db,
        page_title="Portal Activity Log",
        page_subtitle="Customer portal login, logout, and self-service actions",
        entity_type_filter="portal",
    )


def build_email_log_context(request: Request, db: Session) -> dict:
    """Email delivery log."""
    return _build_log_page_context(
        request, db,
        page_title="Email Delivery Log",
        page_subtitle="Outbound email delivery status and tracking",
        entity_type_filter="email",
        action_filter_key="status",
    )


def build_sms_log_context(request: Request, db: Session) -> dict:
    """SMS delivery log."""
    return _build_log_page_context(
        request, db,
        page_title="SMS Delivery Log",
        page_subtitle="SMS message delivery status and error tracking",
        entity_type_filter="sms",
    )


def build_status_changes_context(request: Request, db: Session) -> dict:
    """Subscriber status change history."""
    ctx = _build_log_page_context(
        request, db,
        page_title="Subscriber Status Changes",
        page_subtitle="History of all subscriber status transitions with attribution",
        entity_type_filter="subscriber",
    )
    ctx["action"] = ctx.get("action", "")
    return ctx


def build_service_changes_context(request: Request, db: Session) -> dict:
    """Service/subscription status and plan change log."""
    return _build_log_page_context(
        request, db,
        page_title="Service & Plan Changes",
        page_subtitle="Subscription status transitions and plan migrations",
        entity_type_filter="subscription",
    )


def build_accounting_sync_context(request: Request, db: Session) -> dict:
    """Accounting integration sync log."""
    return _build_log_page_context(
        request, db,
        page_title="Accounting Sync Log",
        page_subtitle="Integration sync status for external accounting systems",
        entity_type_filter="accounting",
        extra_filters={
            "tabs": ["Customers", "Invoices", "Credit Notes", "Payments"],
            "active_tab": request.query_params.get("tab", "Customers"),
        },
    )


def build_payment_gateway_log_context(request: Request, db: Session) -> dict:
    """Payment gateway transaction log."""
    return _build_log_page_context(
        request, db,
        page_title="Payment Gateway Log",
        page_subtitle="Payment provider transaction events and error details",
        entity_type_filter="payment",
        action_filter_key="status",
    )
