"""Customer portal Installation Progress page (project tracker).

Server-rendered: reads the local project mirror (fast, resilient to a CRM
outage) to show the install lifecycle — stage timeline + progress %. Distinct
from /portal/installations (which lists scheduled appointments). Thin wrapper.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import projects_mirror
from app.web.customer.auth import get_current_customer_from_request
from app.web.customer.branding import get_customer_templates

templates = get_customer_templates()
router = APIRouter(prefix="/portal", tags=["web-customer"])
logger = logging.getLogger(__name__)


@router.get("/projects", response_class=HTMLResponse)
def customer_projects(request: Request, db: Session = Depends(get_db)) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/projects", status_code=303
        )
    subscriber_id = str(customer.get("subscriber_id") or "")
    context = {
        "request": request,
        "customer": customer,
        "active_page": "projects",
        "tracker": projects_mirror.read_for_subscriber(db, subscriber_id),
    }
    return templates.TemplateResponse("customer/projects/index.html", context)
