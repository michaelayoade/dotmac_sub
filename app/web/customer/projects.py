"""Customer portal Installation Progress page (project tracker).

Server-rendered: shows the install lifecycle — stage timeline + progress %.
Behind the Phase 3 ``projects_native_read_enabled`` read-flip flag (§4.2):
OFF reads the local project mirror (fast, resilient to a CRM outage), ON
reads the native ``projects`` table — same payload shape (§2.5). Distinct
from /portal/installations (which lists scheduled appointments). Thin wrapper.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import projects as projects_service
from app.services import projects_mirror
from app.services.customer_context import optional_customer_subscriber_id
from app.web.customer.auth import get_current_customer_from_request
from app.web.customer.branding import get_customer_templates

templates = get_customer_templates()
router = APIRouter(prefix="/portal", tags=["web-customer"])
logger = logging.getLogger(__name__)


def _tracker(db: Session, subscriber_id: str) -> dict:
    if projects_service.native_read_enabled(db):
        return projects_service.portal_read_for_subscriber(db, subscriber_id)
    return projects_mirror.read_for_subscriber(db, subscriber_id)


@router.get("/projects", response_class=HTMLResponse)
def customer_projects(request: Request, db: Session = Depends(get_db)) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/projects", status_code=303
        )
    subscriber_id = str(optional_customer_subscriber_id(db, customer) or "")
    context = {
        "request": request,
        "customer": customer,
        "active_page": "projects",
        "tracker": _tracker(db, subscriber_id),
    }
    return templates.TemplateResponse("customer/projects/index.html", context)
