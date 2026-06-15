"""Reseller portal contact management page builders.

Reuses the customer-portal contacts core (``customer_portal_contacts``) keyed on
the reseller's *login subscriber* — the same Subscriber the reseller's saved
cards and email verification key off — so the reseller's contacts are stored and
self-scoped identically to a customer's.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.services import customer_portal_contacts as contacts_service
from app.services import reseller_portal
from app.web.reseller.branding import get_reseller_templates

templates = get_reseller_templates()

RESELLER_LOGIN_URL = "/reseller/auth/login"


def _require_reseller_context(request: Request, db: Session):
    context = reseller_portal.get_context(
        db, request.cookies.get(reseller_portal.SESSION_COOKIE_NAME)
    )
    if not context:
        return None
    return context


def _scoped_customer(context) -> dict:
    """A customer-shaped dict scoped to the reseller's login subscriber.

    Passing only ``subscriber_id`` makes ``customer_portal_contacts`` resolve the
    allowed-subscriber set to exactly that login subscriber, so contacts never
    leak across resellers or into the reseller's managed customer accounts."""
    return {"subscriber_id": str(context["subscriber"].id)}


def _page_context(request: Request, context, db: Session, **extra) -> dict:
    return {
        "request": request,
        "active_page": "contacts",
        "current_user": context["current_user"],
        "reseller": context["reseller"],
        **contacts_service.get_contacts_page(db, _scoped_customer(context)),
        **extra,
    }


def reseller_contacts(request: Request, db: Session):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url=RESELLER_LOGIN_URL, status_code=303)
    return templates.TemplateResponse(
        "reseller/contacts/index.html", _page_context(request, context, db)
    )


def reseller_contacts_create(request: Request, db: Session, **form_fields):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url=RESELLER_LOGIN_URL, status_code=303)
    customer = _scoped_customer(context)
    form = contacts_service.normalize_contact_form(**form_fields)
    try:
        warnings = contacts_service.create_contact(db, customer, form)
    except ValueError as exc:
        return templates.TemplateResponse(
            "reseller/contacts/index.html",
            _page_context(
                request, context, db, error=str(exc), form_values=form
            ),
            status_code=400,
        )
    return templates.TemplateResponse(
        "reseller/contacts/index.html",
        _page_context(
            request, context, db, success="Contact added.", warnings=warnings
        ),
    )


def reseller_contacts_update(
    request: Request, db: Session, contact_id: str, intent, **form_fields
):
    context = _require_reseller_context(request, db)
    if not context:
        return RedirectResponse(url=RESELLER_LOGIN_URL, status_code=303)
    customer = _scoped_customer(context)

    if intent == "delete":
        try:
            contacts_service.delete_contact(db, customer, contact_id)
        except ValueError as exc:
            return templates.TemplateResponse(
                "reseller/contacts/index.html",
                _page_context(request, context, db, error=str(exc)),
                status_code=400,
            )
        return templates.TemplateResponse(
            "reseller/contacts/index.html",
            _page_context(request, context, db, success="Contact deleted."),
        )

    form = contacts_service.normalize_contact_form(**form_fields)
    try:
        warnings = contacts_service.update_contact(db, customer, contact_id, form)
    except ValueError as exc:
        return templates.TemplateResponse(
            "reseller/contacts/index.html",
            _page_context(request, context, db, error=str(exc)),
            status_code=400,
        )
    return templates.TemplateResponse(
        "reseller/contacts/index.html",
        _page_context(
            request, context, db, success="Contact updated.", warnings=warnings
        ),
    )
