"""Customer portal Field Service page (technician visits / work orders).

Server-rendered: reads the local work-order mirror (fast, resilient to a CRM
outage) to show "where's my technician?" — status, scheduled window, ETA, and
the assigned technician. Thin wrapper over the service.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import work_orders_mirror
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
