"""Customer portal Installation Progress page (project tracker).

Server-rendered typed projection of native projects, tasks, field visits and
support resolution. Distinct from /portal/installations (provisioning slots).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import customer_experience_lifecycle
from app.services.customer_context import optional_customer_subscriber_id
from app.web.customer.auth import get_current_customer_from_request
from app.web.customer.branding import get_customer_templates

templates = get_customer_templates()
router = APIRouter(prefix="/portal", tags=["web-customer"])


@router.get("/projects", response_class=HTMLResponse)
def customer_projects(request: Request, db: Session = Depends(get_db)) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/projects", status_code=303
        )
    subscriber_id = optional_customer_subscriber_id(db, customer)
    context = {
        "request": request,
        "customer": customer,
        "active_page": "projects",
        "tracker": customer_experience_lifecycle.projects_for_subscriber(
            db,
            query=customer_experience_lifecycle.CustomerLifecycleQuery(
                UUID(subscriber_id) if subscriber_id else None
            ),
        ),
    }
    return templates.TemplateResponse("customer/projects/index.html", context)
