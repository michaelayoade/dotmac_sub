"""Customer connection-status routes (outage classifier P4 surface).

A selfcare page + a mobile-shaped JSON endpoint answering "what's wrong with my
connection?" — backed by ``connection_status`` (design §P4/§5). Both resolve
ONLY the current session's own subscription, so a customer can never see
another customer's status.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.customer_portal_context import resolve_customer_subscription
from app.services.topology.connection_status import connection_status
from app.web.customer.auth import get_current_customer_from_request
from app.web.customer.branding import get_customer_templates

router = APIRouter(prefix="/portal", tags=["web-customer"])
templates = get_customer_templates()
logger = logging.getLogger(__name__)

# Shown when we can't resolve the customer's subscription (e.g. not provisioned
# yet) — a calm, non-alarming default rather than an error.
_NO_SUBSCRIPTION_STATUS = {
    "state": "connected",
    "headline": "No active service",
    "message": "We couldn't find an active service on your account to check.",
    "advice": None,
    "medium": None,
    "area_outage": False,
    "checked_at": None,
}


def _status_for_request(request: Request, db: Session) -> dict | None:
    """Resolve + diagnose the current customer's own subscription.

    Returns the customer-safe status dict, or ``None`` when unauthenticated.
    """
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return None
    subscription = resolve_customer_subscription(db, customer)
    if subscription is None:
        return dict(_NO_SUBSCRIPTION_STATUS)
    return connection_status(db, subscription)


@router.get("/connection", response_class=HTMLResponse)
def customer_connection_page(
    request: Request, db: Session = Depends(get_db)
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/connection", status_code=303
        )
    status = _status_for_request(request, db) or dict(_NO_SUBSCRIPTION_STATUS)
    return templates.TemplateResponse(
        "customer/connection/index.html",
        {
            "request": request,
            "customer": customer,
            "active_page": "connection",
            "status": status,
        },
    )


@router.get("/connection/status.json")
def customer_connection_status_json(
    request: Request, db: Session = Depends(get_db)
) -> Response:
    """Mobile-shaped JSON: the current customer's own connection status."""
    status = _status_for_request(request, db)
    if status is None:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return JSONResponse(status)
