"""Customer portal Refer & Earn page (RFC #73).

Server-rendered from Sub's native referral source of truth. Individual
subscribers only (the customer portal is subscriber-scoped; reseller float
wallets are never involved).
"""

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import referrals as referrals_service
from app.services.customer_context import optional_customer_subscriber_id
from app.web.customer.auth import get_current_customer_from_request
from app.web.customer.branding import get_customer_templates

templates = get_customer_templates()
router = APIRouter(prefix="/portal", tags=["web-customer"])
logger = logging.getLogger(__name__)

_LOGIN = "/portal/auth/login?next=/portal/refer-and-earn"


def _summary(db: Session, subscriber_id: str) -> dict:
    return referrals_service.referrals.read_for_subscriber(db, subscriber_id)


@router.get("/refer-and-earn", response_class=HTMLResponse)
def customer_refer_and_earn(
    request: Request, db: Session = Depends(get_db)
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url=_LOGIN, status_code=303)

    subscriber_id = str(optional_customer_subscriber_id(db, customer) or "")
    context = {
        "request": request,
        "customer": customer,
        "active_page": "refer-and-earn",
        "referrals": _summary(db, subscriber_id),
        "submitted": request.query_params.get("referred") == "1",
        "form_error": request.query_params.get("error"),
    }
    return templates.TemplateResponse("customer/referrals/index.html", context)


@router.post("/refer-and-earn")
def customer_refer_a_friend(
    request: Request,
    name: str = Form(default=""),
    email: str = Form(default=""),
    phone: str = Form(default=""),
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url=_LOGIN, status_code=303)

    subscriber_id = str(optional_customer_subscriber_id(db, customer) or "")
    try:
        referrals_service.referrals.refer_a_friend(
            db,
            subscriber_id,
            name=name or None,
            email=email or None,
            phone=phone or None,
        )
    except HTTPException as exc:
        message = exc.detail if isinstance(exc.detail, str) else "Referral failed"
        return RedirectResponse(
            url=f"/portal/refer-and-earn?error={quote(message)}", status_code=303
        )
    return RedirectResponse(url="/portal/refer-and-earn?referred=1", status_code=303)
