"""Customer portal Sales/Quotes page (self-serve installation quotes).

Server-rendered from Sub's native quote source of truth.
Read-only — the interactive map-pin request + deposit payment live in the
mobile app. Thin wrapper over the service.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.customer_context import optional_customer_subscriber_id
from app.services.sales import selfserve as selfserve_service
from app.web.customer.auth import get_current_customer_from_request
from app.web.customer.branding import get_customer_templates

templates = get_customer_templates()
router = APIRouter(prefix="/portal", tags=["web-customer"])
logger = logging.getLogger(__name__)


def _quotes(db: Session, subscriber_id: str) -> dict:
    return selfserve_service.selfserve_quotes.read_for_subscriber(db, subscriber_id)


@router.get("/quotes", response_class=HTMLResponse)
def customer_quotes(request: Request, db: Session = Depends(get_db)) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/quotes", status_code=303
        )
    subscriber_id = str(optional_customer_subscriber_id(db, customer) or "")
    context = {
        "request": request,
        "customer": customer,
        "active_page": "quotes",
        "quotes": _quotes(db, subscriber_id),
    }
    return templates.TemplateResponse("customer/quotes/index.html", context)
