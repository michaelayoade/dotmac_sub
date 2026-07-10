"""Customer portal Sales/Quotes page (self-serve installation quotes).

Server-rendered: shows each quote's feasibility, estimate, deposit, and
status. Behind the Phase 3 ``quotes_native_read_enabled`` read-flip flag
(§4.2): OFF reads the local quote mirror (fast, resilient to a CRM outage),
ON reads sub's native ``quotes`` table — same payload shape (§2.5).
Read-only — the interactive map-pin request + deposit payment live in the
mobile app. Thin wrapper over the service.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import quotes_mirror
from app.services.sales import selfserve as selfserve_service
from app.web.customer.auth import get_current_customer_from_request
from app.web.customer.branding import get_customer_templates

templates = get_customer_templates()
router = APIRouter(prefix="/portal", tags=["web-customer"])
logger = logging.getLogger(__name__)


def _quotes(db: Session, subscriber_id: str) -> dict:
    if selfserve_service.native_read_enabled(db):
        return selfserve_service.selfserve_quotes.read_for_subscriber(db, subscriber_id)
    return quotes_mirror.read_for_subscriber(db, subscriber_id)


@router.get("/quotes", response_class=HTMLResponse)
def customer_quotes(request: Request, db: Session = Depends(get_db)) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/quotes", status_code=303
        )
    subscriber_id = str(customer.get("subscriber_id") or "")
    context = {
        "request": request,
        "customer": customer,
        "active_page": "quotes",
        "quotes": _quotes(db, subscriber_id),
    }
    return templates.TemplateResponse("customer/quotes/index.html", context)
