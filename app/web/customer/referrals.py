"""Customer portal Refer & Earn page (RFC #73).

Server-rendered. The page read runs behind the Phase 3
``referrals_native_read_enabled`` read-flip flag (§4.2): OFF reads the local
referral mirror (fast, resilient to a CRM outage), ON reads the native
referral tables — same payload shape (§2.5). The refer-a-friend POST stays a
mirror write-through until the Phase 3 write flip (§4.3, PR 14). Individual
subscribers only (the customer portal is subscriber-scoped; reseller float
wallets are never involved).
"""

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import referrals as referrals_service
from app.services import referrals_mirror
from app.web.customer.auth import get_current_customer_from_request
from app.web.customer.branding import get_customer_templates

templates = get_customer_templates()
router = APIRouter(prefix="/portal", tags=["web-customer"])
logger = logging.getLogger(__name__)

_LOGIN = "/portal/auth/login?next=/portal/refer-and-earn"


def _summary(db: Session, subscriber_id: str) -> dict:
    if referrals_service.native_read_enabled(db):
        return referrals_service.referrals.read_for_subscriber(db, subscriber_id)
    return referrals_mirror.read_for_subscriber(db, subscriber_id)


@router.get("/refer-and-earn", response_class=HTMLResponse)
def customer_refer_and_earn(
    request: Request, db: Session = Depends(get_db)
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url=_LOGIN, status_code=303)

    subscriber_id = str(customer.get("subscriber_id") or "")
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

    subscriber_id = str(customer.get("subscriber_id") or "")
    try:
        referrals_mirror.refer_a_friend(
            db,
            subscriber_id,
            name=name or None,
            email=email or None,
            phone=phone or None,
        )
    except referrals_mirror.ReferralError as exc:
        return RedirectResponse(
            url=f"/portal/refer-and-earn?error={quote(exc.message)}", status_code=303
        )
    return RedirectResponse(url="/portal/refer-and-earn?referred=1", status_code=303)
