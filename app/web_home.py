"""Public landing page — the ISP's customer front door.

Thin adapter per the UI information/action standard: the route resolves the
canonical brand (brand_profiles, platform scope — the owner of identity and
support contact) and renders. This page is public and unauthenticated, so the
context must carry no customer or operational data; the template renders only
brand-owned values and static task navigation.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import brand_profiles
from app.web.templates import templates

router = APIRouter()


@router.get("/", tags=["web"], response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    landing_brand = brand_profiles.resolve_brand(db)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "title": landing_brand.name,
            "landing_brand": landing_brand,
        },
    )
