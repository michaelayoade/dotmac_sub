"""Customer portal Field Service page (technician visits / work orders).

Server-rendered: reads the typed native work-order projection to show schedule,
project/task/ticket context, technician location, and customer feedback.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import customer_experience_lifecycle, customer_work_order_selfcare
from app.services.customer_context import optional_customer_subscriber_id
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.web.customer.auth import get_current_customer_from_request
from app.web.customer.branding import get_customer_templates

templates = get_customer_templates()
router = APIRouter(prefix="/portal", tags=["web-customer"])


@router.get("/work-orders", response_class=HTMLResponse)
def customer_work_orders(request: Request, db: Session = Depends(get_db)) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/work-orders", status_code=303
        )
    subscriber_id = optional_customer_subscriber_id(db, customer)
    context = {
        "request": request,
        "customer": customer,
        "active_page": "work-orders",
        "tracker": customer_experience_lifecycle.work_orders_for_subscriber(
            db,
            query=customer_experience_lifecycle.CustomerLifecycleQuery(
                UUID(subscriber_id) if subscriber_id else None
            ),
        ),
    }
    return templates.TemplateResponse("customer/work_orders/index.html", context)


@router.get("/work-orders/{work_order_id}/technician-location")
def customer_technician_location(
    work_order_id: str, request: Request, db: Session = Depends(get_db)
) -> Response:
    """Live technician position for the map (polled by the page). Same-origin,
    session-authed; returns {available: false} when hidden."""
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    subscriber_id = str(optional_customer_subscriber_id(db, customer) or "")
    data = customer_work_order_selfcare.technician_location(
        db,
        query=customer_work_order_selfcare.TechnicianLocationQuery(
            subscriber_id=UUID(subscriber_id) if subscriber_id else None,
            work_order_public_id=work_order_id,
        ),
    )
    return JSONResponse(data.model_dump(mode="json"))


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
    subscriber_id = optional_customer_subscriber_id(db, customer)
    command = customer_work_order_selfcare.RateTechnicianCommand(
        context=CommandContext.system(
            actor=f"subscriber:{subscriber_id}",
            scope="customer:work_order:rating",
            reason="Customer technician feedback",
        ),
        subscriber_id=UUID(subscriber_id) if subscriber_id else None,
        work_order_public_id=work_order_id,
        rating=max(1, min(5, rating)),
        comment=(comment or "")[:2000] or None,
    )
    db_session_adapter.release_read_transaction(db)
    status = "ok"
    try:
        customer_work_order_selfcare.rate_technician(db, command=command)
    except customer_work_order_selfcare.TechnicianRatingError:
        status = "error"
    return RedirectResponse(url=f"/portal/work-orders?rated={status}", status_code=303)
