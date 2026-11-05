"""Public referral signup page on the same origin as the customer portal."""

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import brand_profiles
from app.services.referrals import referrals
from app.web.templates import templates

router = APIRouter(tags=["web-public-referrals"])
_PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


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
