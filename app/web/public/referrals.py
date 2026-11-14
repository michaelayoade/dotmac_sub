"""Public referral signup page on the same origin as the customer portal."""

from hashlib import sha256

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.request_meta import client_ip
from app.schemas.geocoding import ReverseGeocodeQuery
from app.services import brand_profiles, geocoding
from app.services.rate_limiter_adapter import allow_operation
from app.services.referrals import referrals
from app.web.templates import templates

router = APIRouter(tags=["web-public-referrals"])
_PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


@router.get("/r/{code}/reverse-geocode")
def referral_signup_reverse_geocode(
    request: Request,
    query: ReverseGeocodeQuery = Depends(),
    code: str = Path(min_length=1, max_length=24),
    db: Session = Depends(get_db),
) -> JSONResponse:
    referral_code = referrals.get_by_code(db, code)
    if referral_code is None or not referrals.program(db)["enabled"]:
        return JSONResponse({"detail": "Referral link unavailable"}, status_code=404)
    peer = client_ip(request) or "unknown"
    rate_key = sha256(f"{peer}:{referral_code.code}".encode()).hexdigest()
    if not allow_operation(
        f"referral-signup:reverse-geocode:{rate_key}", limit=10, window_seconds=3600
    ).allowed:
        return JSONResponse({"detail": "Too many location requests"}, status_code=429)
    result = geocoding.resolve_coordinates(db, query)
    if result is None:
        return JSONResponse({"detail": "Address unavailable"}, status_code=404)
    return JSONResponse({"display_name": result.display_name}, headers=_PRIVATE_HEADERS)


@router.get("/r/{code}", response_class=HTMLResponse)
def referral_signup_page(
    request: Request,
    code: str = Path(min_length=1, max_length=24),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    resolved_brand = brand_profiles.resolve_brand(db)
    referral_code = referrals.get_by_code(db, code)
    signup_code = (
        referral_code.code
        if referral_code is not None and referrals.program(db)["enabled"]
        else None
    )
    return templates.TemplateResponse(
        "public/referrals/signup.html",
        {
            "request": request,
            "code": signup_code,
            "brand": resolved_brand,
            "branding_favicon_url": resolved_brand.favicon_url,
        },
        status_code=200 if signup_code is not None else 404,
        headers=_PRIVATE_HEADERS,
    )
