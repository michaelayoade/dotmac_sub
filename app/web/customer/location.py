"""Customer portal service-location page and geocode helpers."""

import logging

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import customer_location_requests as location_service
from app.services import geocoding as geocoding_service
from app.services.customer_context import optional_customer_subscriber_id
from app.web.customer.auth import get_current_customer_from_request
from app.web.customer.branding import get_customer_templates

templates = get_customer_templates()
router = APIRouter(prefix="/portal", tags=["web-customer"])

logger = logging.getLogger(__name__)


def _page_context(request: Request, db: Session, customer: dict) -> dict:
    context = location_service.get_customer_location_page_context(db, customer)
    context.update(
        {
            "request": request,
            "customer": customer,
            "active_page": "location",
            "form_error": None,
            "form_note": "",
        }
    )
    return context


@router.get("/location", response_class=HTMLResponse)
def customer_location_page(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(
            url="/portal/auth/login?next=/portal/location", status_code=303
        )
    context = _page_context(request, db, customer)
    return templates.TemplateResponse("customer/location/index.html", context)


@router.post("/location", response_class=HTMLResponse)
def customer_location_submit(
    request: Request,
    latitude: float = Form(...),
    longitude: float = Form(...),
    customer_note: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    subscriber_id = str(optional_customer_subscriber_id(db, customer) or "")
    try:
        location_service.submit_request(
            db,
            subscriber_id=subscriber_id,
            latitude=latitude,
            longitude=longitude,
            customer_note=customer_note,
            actor_id=subscriber_id or None,
            actor_name=str(customer.get("username") or "") or None,
            submitted_from_ip=request.client.host if request.client else None,
        )
    except Exception as exc:
        detail = getattr(exc, "detail", None) or "Unable to submit the correction."
        context = _page_context(request, db, customer)
        context["form_error"] = str(detail)
        context["form_note"] = customer_note
        return templates.TemplateResponse(
            "customer/location/index.html", context, status_code=400
        )
    return RedirectResponse(url="/portal/location?submitted=1", status_code=303)


@router.post("/location-requests/{request_id}/cancel")
def customer_location_cancel(
    request: Request,
    request_id: str,
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return RedirectResponse(url="/portal/auth/login", status_code=303)
    subscriber_id = str(optional_customer_subscriber_id(db, customer) or "")
    location_service.cancel_request(
        db,
        request_id=request_id,
        subscriber_id=subscriber_id,
        actor_id=subscriber_id or None,
    )
    return RedirectResponse(url="/portal/location?canceled=1", status_code=303)


@router.get("/location/geocode-search")
def customer_location_geocode_search(
    request: Request,
    q: str = Query(min_length=3, max_length=200),
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    results = geocoding_service.geocode_preview(db, {"address_line1": q}, limit=5)
    return JSONResponse(
        [
            {
                "display_name": item.get("display_name"),
                "latitude": item.get("latitude"),
                "longitude": item.get("longitude"),
            }
            for item in results
        ]
    )


@router.get("/location/reverse-geocode")
def customer_location_reverse_geocode(
    request: Request,
    lat: float = Query(),
    lon: float = Query(),
    db: Session = Depends(get_db),
) -> Response:
    customer = get_current_customer_from_request(request, db)
    if not customer:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    result = geocoding_service.reverse_geocode(db, lat, lon)
    if not result:
        return JSONResponse({"display_name": None})
    return JSONResponse(
        {
            "display_name": result.get("display_name"),
            "latitude": result.get("latitude"),
            "longitude": result.get("longitude"),
        }
    )
