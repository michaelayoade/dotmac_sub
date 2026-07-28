"""Customer portal Installation Progress page (project tracker).

Server-rendered typed projection of native projects, tasks, field visits and
support resolution. Distinct from /portal/installations (provisioning slots).
"""

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
    subscriber_id = str(optional_customer_subscriber_id(db, customer) or "")
    context = {
        "request": request,
        "customer": customer,
        "active_page": "projects",
        "tracker": customer_experience_lifecycle.projects_for_subscriber(
            db, subscriber_id
        ),
    }
    return templates.TemplateResponse("customer/projects/index.html", context)
