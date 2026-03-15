"""Service helpers for reseller portal routes."""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.services import customer_portal, reseller_portal

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")


def _require_reseller_context(request: Request, db: Session):
    context = reseller_portal.get_context(
        db, request.cookies.get(reseller_portal.SESSION_COOKIE_NAME)
    )
    if not context:
        return None
    return context


def reseller_home(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    return RedirectResponse(url="/reseller/dashboard", status_code=303)


def reseller_dashboard(
    request: Request,
    db: Session,
    page: int,
    per_page: int,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    offset = (page - 1) * per_page
    summary = reseller_portal.get_dashboard_summary(
        db,
        reseller_id=str(context["reseller"].id),
        limit=per_page,
        offset=offset,
    )

    return templates.TemplateResponse(
        "reseller/dashboard/index.html",
        {
            "request": request,
            "active_page": "dashboard",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "summary": summary,
            "page": page,
            "per_page": per_page,
        },
    )


def reseller_accounts(
    request: Request,
    db: Session,
    page: int,
    per_page: int,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    offset = (page - 1) * per_page
    accounts = reseller_portal.list_accounts(
        db,
        reseller_id=str(context["reseller"].id),
        limit=per_page,
        offset=offset,
    )
    return templates.TemplateResponse(
        "reseller/accounts/index.html",
        {
            "request": request,
            "active_page": "accounts",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            "accounts": accounts,
            "page": page,
            "per_page": per_page,
        },
    )


def reseller_account_view(
    request: Request,
    db: Session,
    account_id: str,
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)

    # Note: underlying service function name is legacy misspelling.
    session_token = reseller_portal.create_customer_imsubscriberation_session(
        db=db,
        reseller_id=str(context["reseller"].id),
        account_id=account_id,
        return_to="/reseller/accounts",
    )
    response = RedirectResponse(url="/portal/dashboard", status_code=303)
    response.set_cookie(
        key=customer_portal.SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=customer_portal.get_session_max_age(db),
    )
    return response


def reseller_fiber_map(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url="/reseller/auth/login", status_code=303)
    from app.services import web_network_fiber as fiber_service

    map_data = fiber_service.get_fiber_plant_map_data(db)

    return templates.TemplateResponse(
        "reseller/network/fiber-map.html",
        {
            "request": request,
            "active_page": "fiber-map",
            "current_user": context["current_user"],
            "reseller": context["reseller"],
            **map_data,
            "read_only": True,
        },
    )
