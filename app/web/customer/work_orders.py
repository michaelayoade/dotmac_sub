"""Customer portal Field Service page (technician visits / work orders).

Server-rendered: reads the local work-order mirror (fast, resilient to a CRM
outage) to show "where's my technician?" — status, scheduled window, ETA, and
the assigned technician. Live technician position + rating proxy the CRM via
same-origin (session-authed) routes so the browser needs no bearer token.
"""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import crm_portal as crm_portal_service
from app.services import work_orders_mirror
from app.services.crm_client import get_crm_client
from app.web.customer.auth import get_current_customer_from_request
from app.web.customer.branding import get_customer_templates

templates = get_customer_templates()
router = APIRouter(prefix="/portal", tags=["web-customer"])
logger = logging.getLogger(__name__)


@router.get("/work-orders", response_class=HTMLResponse)
def customer_work_orders(request: Request, db: Session = Depends(get_db)) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/work-orders", status_code=303
        )
    subscriber_id = str(customer.get("subscriber_id") or "")
    context = {
        "request": request,
        "customer": customer,
        "active_page": "work-orders",
        "tracker": work_orders_mirror.read_for_subscriber(db, subscriber_id),
    }
    return templates.TemplateResponse("customer/work_orders/index.html", context)


@router.get("/work-orders/{work_order_id}/technician-location")
def customer_technician_location(
    work_order_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    """Live technician position for the map (polled by the page). Same-origin,
    session-authed; proxies the CRM. Returns {available: false} when hidden."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    subscriber_id = str(customer.get("subscriber_id") or "")
    crm_id = crm_portal_service.resolve_crm_subscriber_id(db, subscriber_id)
    if not crm_id:
        return JSONResponse({"available": False, "reason": "not_linked"})
    try:
        data = get_crm_client(db).get_portal_technician_location(crm_id, work_order_id)
    except Exception:  # noqa: BLE001 - live position is best-effort
        logger.warning("technician_location_proxy_failed wo=%s", work_order_id)
        return JSONResponse({"available": False, "reason": "error"})
    return JSONResponse(data)


@router.post("/work-orders/{work_order_id}/rate-technician")
def customer_rate_technician(
    work_order_id: str,
    request: Request,
    rating: int = Form(...),
    comment: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    """Submit a technician rating (standard form POST + csrf_input), then redirect
    back to the visits page with a toast flag."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/work-orders", status_code=303
        )
    subscriber_id = str(customer.get("subscriber_id") or "")
    crm_id = crm_portal_service.resolve_crm_subscriber_id(db, subscriber_id)
    status = "ok"
    if not crm_id:
        status = "error"
    else:
        try:
            get_crm_client(db).submit_portal_technician_rating(
                crm_id,
                work_order_id,
                rating=max(1, min(5, rating)),
                comment=comment or None,
            )
        except Exception:  # noqa: BLE001 - rating is best-effort
            logger.warning("technician_rating_proxy_failed wo=%s", work_order_id)
            status = "error"
    return RedirectResponse(url=f"/portal/work-orders?rated={status}", status_code=303)
