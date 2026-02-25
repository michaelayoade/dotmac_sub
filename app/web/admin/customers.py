"""Admin customer (person & organization) management web routes."""

import json
import logging
import uuid

from fastapi import (
    APIRouter,
    Body,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import customer_portal
from app.services import subscriber as subscriber_service
from app.services import web_customer_actions as web_customer_actions_service
from app.services import web_customer_details as web_customer_details_service
from app.services import web_customer_lists as web_customer_lists_service
from app.services import web_customer_user_access as web_customer_user_access_service
from app.services import web_system_user_mutations as web_system_user_mutations_service
from app.services.audit_helpers import (
    build_changes_metadata,
    log_audit_event,
)
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_json_body

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/customers", tags=["web-admin-customers"])
contacts_router = APIRouter(prefix="/contacts", tags=["web-admin-contacts"])


def _parse_json(value: str | None, field: str) -> dict | None:
    if not value or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field} must be a JSON object")
    return parsed


def _htmx_error_response(
    message: str,
    status_code: int = 409,
    title: str = "Delete blocked",
    reswap: str | None = None,
) -> Response:
    trigger = {
        "showToast": {
            "type": "error",
            "title": title,
            "message": message,
        }
    }
    headers = {"HX-Trigger": json.dumps(trigger)}
    if reswap:
        headers["HX-Reswap"] = reswap
    return Response(status_code=status_code, headers=headers)


def _get_subscriber(db: Session, subscriber_id: str):
    return subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)


def _toast_response(
    *,
    request: Request,
    redirect_url: str,
    ok: bool,
    title: str,
    message: str,
) -> Response:
    trigger = {
        "showToast": {
            "type": "success" if ok else "error",
            "title": title,
            "message": message,
            "duration": 8000,
        }
    }
    if request.headers.get("HX-Request"):
        headers = {"HX-Trigger": json.dumps(trigger), "HX-Refresh": "true"}
        return Response(status_code=204, headers=headers)
    return RedirectResponse(url=redirect_url, status_code=303)


def _contacts_base_context(request: Request, db: Session, active_page: str = "contacts"):
    """Base context for contacts pages."""
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@contacts_router.get("", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:read"))])
def contacts_list(
    request: Request,
    search: str | None = None,
    status: str | None = None,  # 'lead', 'contact', 'customer', or None for all
    entity_type: str | None = None,  # 'person' or 'organization'
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """Unified contacts view - all people and organizations with status filtering."""
    context = _contacts_base_context(request, db, "contacts")
    context.update(
        web_customer_lists_service.build_contacts_index_context(
            db=db,
            search=search,
            status=status,
            entity_type=entity_type,
            page=page,
            per_page=per_page,
        )
    )
    # HTMX requests should return only the table+pagination partial.
    template_name = "admin/contacts/_table.html" if request.headers.get("HX-Request") == "true" else "admin/contacts/index.html"
    return templates.TemplateResponse(template_name, context)


@contacts_router.get("/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:read"))])
def contacts_new_redirect() -> RedirectResponse:
    return RedirectResponse(url="/admin/crm/contacts/new", status_code=302)


@contacts_router.post("/{person_id}/convert", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:write"))])
def contacts_convert_to_subscriber(
    request: Request,
    person_id: uuid.UUID,
    subscriber_type: str | None = Form("person"),
    account_status: str | None = Form("active"),
    db: Session = Depends(get_db),
):
    """Convert a person contact into an active subscriber."""
    person, missing_email = web_customer_actions_service.convert_contact_to_subscriber(
        db=db,
        person_id=person_id,
        account_status=account_status,
    )
    # Log unsupported subscriber_type without changing behavior.
    if subscriber_type and subscriber_type != "person":
        logger.info(
            "Unsupported subscriber_type",
            extra={"subscriber_type": subscriber_type, "person_id": str(person.id)},
        )
    redirect_url = f"/admin/subscribers/{person.id}"
    if missing_email:
        redirect_url = f"{redirect_url}?missing_email=1"
    return RedirectResponse(url=redirect_url, status_code=303)


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:read"))])
def customers_list(
    request: Request,
    search: str | None = None,
    customer_type: str | None = None,  # 'person' or 'organization'
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List all customers (people and organizations) with search and filtering."""
    page_data = web_customer_lists_service.build_customers_index_context(
        db=db,
        search=search,
        customer_type=customer_type,
        page=page,
        per_page=per_page,
    )

    # Check if this is an HTMX request for table body only
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "admin/customers/_table.html",
            {
                "request": request,
                **page_data,
            },
        )

    # Get sidebar stats and current user
    from app.web.admin import get_current_user, get_sidebar_stats
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/customers/index.html",
        {
            "request": request,
            **page_data,
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


# Note: /new routes must be defined BEFORE /{customer_id} to avoid path matching issues

@router.get("/wizard", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:write"))])
def customer_wizard_form(
    request: Request,
    db: Session = Depends(get_db),
):
    """Customer creation wizard (multi-step form)."""
    from app.services.smart_defaults import SmartDefaultsService
    from app.web.admin import get_current_user, get_sidebar_stats

    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    # Get default country from settings
    defaults_service = SmartDefaultsService(db)
    customer_defaults = defaults_service.get_customer_defaults("person")

    return templates.TemplateResponse(
        "admin/customers/form_wizard.html",
        {
            "request": request,
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
            "default_country": customer_defaults.get("country_code", "NG"),
        },
    )


@router.post("/wizard", dependencies=[Depends(require_permission("customer:write"))])
def customer_wizard_create(
    request: Request,
    data: dict = Body(...),
    db: Session = Depends(get_db),
):
    """Create a customer from wizard (JSON submission)."""
    try:
        created_type, created_id = web_customer_actions_service.create_customer_from_wizard(
            db=db,
            data=data,
        )
        return {"success": True, "redirect": f"/admin/customers/{created_type}/{created_id}"}

    except ValueError as exc:
        return {"success": False, "message": str(exc)}
    except IntegrityError:
        db.rollback()
        return {"success": False, "message": "A customer with this information already exists."}
    except Exception as exc:
        return {"success": False, "message": f"An error occurred: {str(exc)}"}


@router.get("/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:write"))])
def customer_new(
    request: Request,
    type: str | None = "person",
    db: Session = Depends(get_db),
):
    """New customer form."""
    from app.web.admin import get_current_user, get_sidebar_stats
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/customers/form.html",
        {
            "request": request,
            "customer": None,
            "customer_type": type,
            "action": "create",
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.post("/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:write"))])
def customer_create(
    request: Request,
    customer_type: str = Form(...),
    # Subscriber fields
    first_name: str | None = Form(None),
    last_name: str | None = Form(None),
    display_name: str | None = Form(None),
    avatar_url: str | None = Form(None),
    bio: str | None = Form(None),
    # Organization fields
    name: str | None = Form(None),
    legal_name: str | None = Form(None),
    tax_id: str | None = Form(None),
    domain: str | None = Form(None),
    website: str | None = Form(None),
    org_notes: str | None = Form(None),
    # Common fields
    email: str | None = Form(None),
    email_verified: str | None = Form(None),
    phone: str | None = Form(None),
    date_of_birth: str | None = Form(None),
    gender: str | None = Form(None),
    preferred_contact_method: str | None = Form(None),
    locale: str | None = Form(None),
    timezone: str | None = Form(None),
    address_line1: str | None = Form(None),
    address_line2: str | None = Form(None),
    city: str | None = Form(None),
    region: str | None = Form(None),
    postal_code: str | None = Form(None),
    country_code: str | None = Form(None),
    status: str | None = Form(None),
    is_active: str | None = Form(None),
    marketing_opt_in: str | None = Form(None),
    notes: str | None = Form(None),
    account_start_date: str | None = Form(None),
    org_account_start_date: str | None = Form(None),
    metadata: str | None = Form(None),
    contact_first_name: list[str] = Form([]),
    contact_last_name: list[str] = Form([]),
    contact_title: list[str] = Form([]),
    contact_role: list[str] = Form([]),
    contact_email: list[str] = Form([]),
    contact_phone: list[str] = Form([]),
    contact_is_primary: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    """Create a new customer (person or organization)."""
    try:
        contact_columns = {
            "first_name": contact_first_name,
            "last_name": contact_last_name,
            "title": contact_title,
            "role": contact_role,
            "email": contact_email,
            "phone": contact_phone,
            "is_primary": contact_is_primary,
        }
        form_data = {
            "first_name": first_name,
            "last_name": last_name,
            "display_name": display_name,
            "avatar_url": avatar_url,
            "bio": bio,
            "name": name,
            "legal_name": legal_name,
            "tax_id": tax_id,
            "domain": domain,
            "website": website,
            "org_notes": org_notes,
            "email": email,
            "email_verified": email_verified,
            "phone": phone,
            "date_of_birth": date_of_birth,
            "gender": gender,
            "preferred_contact_method": preferred_contact_method,
            "locale": locale,
            "timezone": timezone,
            "address_line1": address_line1,
            "address_line2": address_line2,
            "city": city,
            "region": region,
            "postal_code": postal_code,
            "country_code": country_code,
            "status": status,
            "is_active": is_active,
            "marketing_opt_in": marketing_opt_in,
            "notes": notes,
            "account_start_date": account_start_date,
            "org_account_start_date": org_account_start_date,
            "metadata_json": _parse_json(metadata, "metadata"),
        }
        created_type, created_id = web_customer_actions_service.create_customer_from_form(
            db=db,
            customer_type=customer_type,
            form_data=form_data,
            contact_columns=contact_columns,
        )

        return RedirectResponse(
            url=f"/admin/customers/{created_type}/{created_id}",
            status_code=303,
        )
    except HTTPException:
        raise
    except Exception as e:
        # Ensure failed transactions don't break error-page queries/rendering.
        db.rollback()
        from app.web.admin import get_current_user, get_sidebar_stats
        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        contact_rows = []
        try:
            contact_rows = web_customer_actions_service.build_error_contact_rows(
                {
                    "first_name": contact_first_name,
                    "last_name": contact_last_name,
                    "title": contact_title,
                    "role": contact_role,
                    "email": contact_email,
                    "phone": contact_phone,
                    "is_primary": contact_is_primary,
                }
            )
        except Exception:
            contact_rows = []
        return templates.TemplateResponse(
            "admin/customers/form.html",
            {
                "request": request,
                "customer": None,
                "customer_type": customer_type,
                "action": "create",
                "error": str(e),
                "form": {
                    "contact_rows": contact_rows or None,
                },
                "current_user": current_user,
                "sidebar_stats": sidebar_stats,
            },
            status_code=400,
        )


@router.get("/person/{customer_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:read"))])
def person_detail(
    request: Request,
    customer_id: str,
    db: Session = Depends(get_db),
):
    """View person details."""
    try:
        detail_data = web_customer_details_service.build_person_detail_snapshot(
            db=db,
            customer_id=customer_id,
        )
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Customer not found"},
            status_code=404,
        )

    from app.web.admin import get_current_user, get_sidebar_stats
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/customers/detail.html",
        {
            "request": request,
            **detail_data,
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.get("/organization/{customer_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:read"))])
def organization_detail(
    request: Request,
    customer_id: str,
    db: Session = Depends(get_db),
):
    """View organization details."""
    try:
        detail_data = web_customer_details_service.build_organization_detail_snapshot(
            db=db,
            customer_id=customer_id,
        )
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Organization not found"},
            status_code=404,
        )

    from app.web.admin import get_current_user, get_sidebar_stats
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/customers/detail.html",
        {
            "request": request,
            **detail_data,
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.post(
    "/{customer_type}/{customer_id}/user/invite",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:read"))],
)
def customer_user_send_invite(
    request: Request,
    customer_type: str,
    customer_id: str,
    db: Session = Depends(get_db),
):
    redirect_url = f"/admin/customers/{customer_type}/{customer_id}"
    from app.web.admin import get_current_user

    actor = get_current_user(request)
    actor_id = str(actor.get("subscriber_id")) if actor else None
    try:
        state = web_customer_user_access_service.build_customer_user_access_state(
            db,
            customer_type=customer_type,
            customer_id=customer_id,
        )
        if not state.get("can_send_invite"):
            retry_at = state.get("invite_available_at")
            when = retry_at.strftime("%Y-%m-%d %H:%M UTC") if retry_at else "later"
            message = f"Invite already sent recently. You can resend after {when}."
            log_audit_event(
                db=db,
                request=request,
                action=web_customer_user_access_service.INVITE_AUDIT_ACTION,
                entity_type="subscriber",
                entity_id=str(state.get("target_subscriber_id") or ""),
                actor_id=actor_id,
                metadata={"reason": "rate_limited"},
                status_code=429,
                is_success=False,
            )
            return _toast_response(
                request=request,
                redirect_url=redirect_url,
                ok=False,
                title="Invite blocked",
                message=message,
            )
        note = web_system_user_mutations_service.send_user_invite_for_user(
            db,
            user_id=str(state["target_subscriber_id"]),
        )
        log_audit_event(
            db=db,
            request=request,
            action=web_customer_user_access_service.INVITE_AUDIT_ACTION,
            entity_type="subscriber",
            entity_id=str(state["target_subscriber_id"]),
            actor_id=actor_id,
            metadata={
                "email": state.get("email"),
                "email_source": state.get("email_source"),
                "customer_type": customer_type,
                "result": note,
            },
            status_code=200,
            is_success="sent" in note.lower(),
        )
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok="sent" in note.lower(),
            title="User invite",
            message=note,
        )
    except Exception as exc:
        log_audit_event(
            db=db,
            request=request,
            action=web_customer_user_access_service.INVITE_AUDIT_ACTION,
            entity_type="customer",
            entity_id=str(customer_id),
            actor_id=actor_id,
            metadata={"customer_type": customer_type, "error": str(exc)},
            status_code=500,
            is_success=False,
        )
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=False,
            title="User invite",
            message=str(exc),
        )


@router.post(
    "/{customer_type}/{customer_id}/user/reset-link",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:read"))],
)
def customer_user_send_reset_link(
    request: Request,
    customer_type: str,
    customer_id: str,
    db: Session = Depends(get_db),
):
    redirect_url = f"/admin/customers/{customer_type}/{customer_id}"
    from app.web.admin import get_current_user

    actor = get_current_user(request)
    actor_id = str(actor.get("subscriber_id")) if actor else None
    try:
        state = web_customer_user_access_service.build_customer_user_access_state(
            db,
            customer_type=customer_type,
            customer_id=customer_id,
        )
        if not state.get("can_send_reset"):
            message = "Reset limit reached: max 3 reset links per hour."
            log_audit_event(
                db=db,
                request=request,
                action=web_customer_user_access_service.RESET_AUDIT_ACTION,
                entity_type="subscriber",
                entity_id=str(state.get("target_subscriber_id") or ""),
                actor_id=actor_id,
                metadata={"reason": "rate_limited"},
                status_code=429,
                is_success=False,
            )
            return _toast_response(
                request=request,
                redirect_url=redirect_url,
                ok=False,
                title="Reset link blocked",
                message=message,
            )
        note = web_system_user_mutations_service.send_password_reset_link_for_user(
            db,
            user_id=str(state["target_subscriber_id"]),
        )
        log_audit_event(
            db=db,
            request=request,
            action=web_customer_user_access_service.RESET_AUDIT_ACTION,
            entity_type="subscriber",
            entity_id=str(state["target_subscriber_id"]),
            actor_id=actor_id,
            metadata={
                "email": state.get("email"),
                "email_source": state.get("email_source"),
                "customer_type": customer_type,
                "result": note,
            },
            status_code=200,
            is_success="sent" in note.lower(),
        )
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok="sent" in note.lower(),
            title="Password reset",
            message=note,
        )
    except Exception as exc:
        log_audit_event(
            db=db,
            request=request,
            action=web_customer_user_access_service.RESET_AUDIT_ACTION,
            entity_type="customer",
            entity_id=str(customer_id),
            actor_id=actor_id,
            metadata={"customer_type": customer_type, "error": str(exc)},
            status_code=500,
            is_success=False,
        )
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=False,
            title="Password reset",
            message=str(exc),
        )


@router.post(
    "/{customer_type}/{customer_id}/user/activate-login",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:read"))],
)
def customer_user_activate_login(
    request: Request,
    customer_type: str,
    customer_id: str,
    db: Session = Depends(get_db),
):
    redirect_url = f"/admin/customers/{customer_type}/{customer_id}"
    from app.web.admin import get_current_user

    actor = get_current_user(request)
    actor_id = str(actor.get("subscriber_id")) if actor else None
    try:
        target = web_customer_user_access_service.activate_customer_login(
            db,
            customer_type=customer_type,
            customer_id=customer_id,
        )
        log_audit_event(
            db=db,
            request=request,
            action=web_customer_user_access_service.LOGIN_TOGGLE_AUDIT_ACTION,
            entity_type="subscriber",
            entity_id=str(target.subscriber.id),
            actor_id=actor_id,
            metadata={"login_active": True, "customer_type": customer_type},
        )
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=True,
            title="Login activated",
            message="Customer portal login has been activated.",
        )
    except Exception as exc:
        log_audit_event(
            db=db,
            request=request,
            action=web_customer_user_access_service.LOGIN_TOGGLE_AUDIT_ACTION,
            entity_type="customer",
            entity_id=str(customer_id),
            actor_id=actor_id,
            metadata={"customer_type": customer_type, "login_active": True, "error": str(exc)},
            status_code=500,
            is_success=False,
        )
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=False,
            title="Login activation",
            message=str(exc),
        )


@router.post(
    "/{customer_type}/{customer_id}/user/deactivate-login",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:read"))],
)
def customer_user_deactivate_login(
    request: Request,
    customer_type: str,
    customer_id: str,
    db: Session = Depends(get_db),
):
    redirect_url = f"/admin/customers/{customer_type}/{customer_id}"
    from app.web.admin import get_current_user

    actor = get_current_user(request)
    actor_id = str(actor.get("subscriber_id")) if actor else None
    try:
        target = web_customer_user_access_service.deactivate_customer_login(
            db,
            customer_type=customer_type,
            customer_id=customer_id,
        )
        log_audit_event(
            db=db,
            request=request,
            action=web_customer_user_access_service.LOGIN_TOGGLE_AUDIT_ACTION,
            entity_type="subscriber",
            entity_id=str(target.subscriber.id),
            actor_id=actor_id,
            metadata={"login_active": False, "customer_type": customer_type},
        )
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=True,
            title="Login deactivated",
            message="Customer portal login has been deactivated.",
        )
    except Exception as exc:
        log_audit_event(
            db=db,
            request=request,
            action=web_customer_user_access_service.LOGIN_TOGGLE_AUDIT_ACTION,
            entity_type="customer",
            entity_id=str(customer_id),
            actor_id=actor_id,
            metadata={"customer_type": customer_type, "login_active": False, "error": str(exc)},
            status_code=500,
            is_success=False,
        )
        return _toast_response(
            request=request,
            redirect_url=redirect_url,
            ok=False,
            title="Login deactivation",
            message=str(exc),
        )


@router.post("/person/{customer_id}/impersonate", response_class=HTMLResponse)
def person_impersonate(
    request: Request,
    customer_id: str,
    account_id: str = Form(...),
    subscription_id: str | None = Form(None),
    db: Session = Depends(get_db),
    auth=Depends(require_permission("subscriber:impersonate")),
):
    """Impersonate a person customer and open the portal."""
    try:
        session_token = web_customer_actions_service.create_impersonation_session(
            db=db,
            request=request,
            customer_type="person",
            customer_id=customer_id,
            account_id=account_id,
            subscription_id=subscription_id,
            auth=auth,
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": str(exc.detail)},
            status_code=exc.status_code,
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


@router.post("/organization/{customer_id}/impersonate", response_class=HTMLResponse)
def organization_impersonate(
    request: Request,
    customer_id: str,
    account_id: str = Form(...),
    subscription_id: str | None = Form(None),
    db: Session = Depends(get_db),
    auth=Depends(require_permission("subscriber:impersonate")),
):
    """Impersonate an organization customer and open the portal."""
    try:
        session_token = web_customer_actions_service.create_impersonation_session(
            db=db,
            request=request,
            customer_type="organization",
            customer_id=customer_id,
            account_id=account_id,
            subscription_id=subscription_id,
            auth=auth,
        )
    except HTTPException as exc:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": str(exc.detail)},
            status_code=exc.status_code,
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


@router.get("/person/{customer_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:write"))])
def person_edit(
    request: Request,
    customer_id: str,
    db: Session = Depends(get_db),
):
    """Edit person form."""
    try:
        customer = _get_subscriber(db=db, subscriber_id=customer_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Customer not found"},
            status_code=404,
        )

    from app.web.admin import get_current_user, get_sidebar_stats
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/customers/form.html",
        {
            "request": request,
            "customer": customer,
            "customer_type": "person",
            "action": "edit",
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.get("/organization/{customer_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:write"))])
def organization_edit(
    request: Request,
    customer_id: str,
    db: Session = Depends(get_db),
):
    """Edit organization form."""
    try:
        customer = subscriber_service.organizations.get(db=db, organization_id=customer_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Organization not found"},
            status_code=404,
        )

    from app.web.admin import get_current_user, get_sidebar_stats
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/customers/form.html",
        {
            "request": request,
            "customer": customer,
            "customer_type": "organization",
            "action": "edit",
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.post("/person/{customer_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:write"))])
def person_update(
    request: Request,
    customer_id: str,
    first_name: str = Form(...),
    last_name: str = Form(...),
    display_name: str | None = Form(None),
    avatar_url: str | None = Form(None),
    bio: str | None = Form(None),
    email: str | None = Form(None),
    email_verified: str | None = Form(None),
    phone: str | None = Form(None),
    date_of_birth: str | None = Form(None),
    gender: str | None = Form(None),
    preferred_contact_method: str | None = Form(None),
    locale: str | None = Form(None),
    timezone: str | None = Form(None),
    address_line1: str | None = Form(None),
    address_line2: str | None = Form(None),
    city: str | None = Form(None),
    region: str | None = Form(None),
    postal_code: str | None = Form(None),
    country_code: str | None = Form(None),
    status: str | None = Form(None),
    is_active: str | None = Form(None),
    marketing_opt_in: str | None = Form(None),
    notes: str | None = Form(None),
    account_start_date: str | None = Form(None),
    metadata: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Update a person."""
    try:
        before, after = web_customer_actions_service.update_person_customer(
            db=db,
            customer_id=customer_id,
            first_name=first_name,
            last_name=last_name,
            display_name=display_name,
            avatar_url=avatar_url,
            email=email,
            email_verified=email_verified,
            phone=phone,
            date_of_birth=date_of_birth,
            gender=gender,
            preferred_contact_method=preferred_contact_method,
            locale=locale,
            timezone_value=timezone,
            address_line1=address_line1,
            address_line2=address_line2,
            city=city,
            region=region,
            postal_code=postal_code,
            country_code=country_code,
            status=status,
            is_active=is_active,
            marketing_opt_in=marketing_opt_in,
            notes=notes,
            account_start_date=account_start_date,
            metadata_json=_parse_json(metadata, "metadata") if metadata is not None else None,
        )
        metadata_payload = build_changes_metadata(before, after)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="subscriber",
            entity_id=str(customer_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata_payload,
        )
        return RedirectResponse(
            url=f"/admin/customers/person/{customer_id}",
            status_code=303,
        )
    except HTTPException:
        raise
    except Exception as e:
        from app.web.admin import get_current_user, get_sidebar_stats
        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        try:
            customer = _get_subscriber(db=db, subscriber_id=customer_id)
        except Exception:
            customer = None
        return templates.TemplateResponse(
            "admin/customers/form.html",
            {
                "request": request,
                "customer": customer,
                "customer_type": "person",
                "action": "edit",
                "error": str(e),
                "current_user": current_user,
                "sidebar_stats": sidebar_stats,
            },
            status_code=400,
        )


@router.post("/organization/{customer_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:write"))])
def organization_update(
    request: Request,
    customer_id: str,
    name: str = Form(...),
    legal_name: str | None = Form(None),
    tax_id: str | None = Form(None),
    domain: str | None = Form(None),
    website: str | None = Form(None),
    org_notes: str | None = Form(None),
    org_account_start_date: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Update an organization."""
    try:
        before, after = web_customer_actions_service.update_organization_customer(
            db=db,
            customer_id=customer_id,
            name=name,
            legal_name=legal_name,
            tax_id=tax_id,
            domain=domain,
            website=website,
            org_notes=org_notes,
            org_account_start_date=org_account_start_date,
        )
        metadata_payload = build_changes_metadata(before, after)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="organization",
            entity_id=str(customer_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata_payload,
        )
        return RedirectResponse(
            url=f"/admin/customers/organization/{customer_id}",
            status_code=303,
        )
    except Exception as e:
        from app.web.admin import get_current_user, get_sidebar_stats
        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        try:
            customer = subscriber_service.organizations.get(db=db, organization_id=customer_id)
        except Exception:
            customer = None
        return templates.TemplateResponse(
            "admin/customers/form.html",
            {
                "request": request,
                "customer": customer,
                "customer_type": "organization",
                "action": "edit",
                "error": str(e),
                "current_user": current_user,
                "sidebar_stats": sidebar_stats,
            },
            status_code=400,
        )


@router.post("/person/{customer_id}/deactivate", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:write"))])
def person_deactivate(
    request: Request,
    customer_id: str,
    db: Session = Depends(get_db),
):
    """Deactivate a person before deletion."""
    before, after = web_customer_actions_service.deactivate_person_customer(
        db=db,
        customer_id=customer_id,
    )
    metadata_payload = build_changes_metadata(before, after)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="subscriber",
        entity_id=str(customer_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata_payload,
    )
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    return RedirectResponse(url=f"/admin/customers/person/{customer_id}", status_code=303)


@router.post("/organization/{customer_id}/deactivate", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:write"))])
def organization_deactivate(
    request: Request,
    customer_id: str,
    db: Session = Depends(get_db),
):
    """Deactivate organization subscribers before deletion."""
    web_customer_actions_service.deactivate_organization_customer(
        db=db,
        customer_id=customer_id,
    )
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="organization",
        entity_id=str(customer_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"changes": {"is_active": {"from": True, "to": False}}},
    )
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    return RedirectResponse(url=f"/admin/customers/organization/{customer_id}", status_code=303)


@router.delete("/person/{customer_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:delete"))])
@router.post("/person/{customer_id}/delete", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:delete"))])
def person_delete(
    request: Request,
    customer_id: str,
    db: Session = Depends(get_db),
):
    """Delete a person."""
    try:
        web_customer_actions_service.delete_person_customer(db=db, customer_id=customer_id)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="delete",
            entity_type="subscriber",
            entity_id=str(customer_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        )
        if request.headers.get("HX-Request"):
            return HTMLResponse(content="", headers={"HX-Redirect": "/admin/customers"})
        return RedirectResponse(url="/admin/customers", status_code=303)
    except HTTPException as exc:
        if request.headers.get("HX-Request"):
            return _htmx_error_response(str(exc.detail), status_code=200, reswap="none")
        raise
    except IntegrityError:
        db.rollback()
        message = "Cannot delete customer. Linked records exist."
        if request.headers.get("HX-Request"):
            return _htmx_error_response(message, status_code=200, reswap="none")
        raise HTTPException(status_code=409, detail=message)
    except Exception as e:
        from app.web.admin import get_current_user, get_sidebar_stats
        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        return templates.TemplateResponse(
            "admin/errors/500.html",
            {"request": request, "error": str(e), "current_user": current_user, "sidebar_stats": sidebar_stats},
            status_code=500,
        )


@router.delete("/organization/{customer_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:delete"))])
@router.post("/organization/{customer_id}/delete", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:delete"))])
def organization_delete(
    request: Request,
    customer_id: str,
    db: Session = Depends(get_db),
):
    """Delete an organization."""
    try:
        web_customer_actions_service.delete_organization_customer(
            db=db,
            customer_id=customer_id,
        )
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="delete",
            entity_type="organization",
            entity_id=str(customer_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        )
        if request.headers.get("HX-Request"):
            return HTMLResponse(content="", headers={"HX-Redirect": "/admin/customers"})
        return RedirectResponse(url="/admin/customers", status_code=303)
    except HTTPException as exc:
        if request.headers.get("HX-Request"):
            return _htmx_error_response(str(exc.detail), status_code=200, reswap="none")
        raise
    except IntegrityError:
        db.rollback()
        message = "Cannot delete organization. Linked records exist."
        if request.headers.get("HX-Request"):
            return _htmx_error_response(message, status_code=200, reswap="none")
        raise HTTPException(status_code=409, detail=message)
    except Exception as e:
        from app.web.admin import get_current_user, get_sidebar_stats
        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        return templates.TemplateResponse(
            "admin/errors/500.html",
            {"request": request, "error": str(e), "current_user": current_user, "sidebar_stats": sidebar_stats},
            status_code=500,
        )


# ============================================================================
# Address Management Routes
# ============================================================================

@router.post("/addresses", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:write"))])
def create_address(
    request: Request,
    subscriber_id: str = Form(...),
    customer_type: str = Form(...),
    customer_id: str = Form(...),
    address_type: str = Form("service"),
    label: str | None = Form(None),
    address_line1: str = Form(...),
    address_line2: str | None = Form(None),
    city: str | None = Form(None),
    region: str | None = Form(None),
    postal_code: str | None = Form(None),
    country_code: str | None = Form(None),
    is_primary: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Create a new address for a subscriber."""
    try:
        web_customer_actions_service.create_customer_address(
            db=db,
            subscriber_id=subscriber_id,
            address_type=address_type,
            label=label,
            address_line1=address_line1,
            address_line2=address_line2,
            city=city,
            region=region,
            postal_code=postal_code,
            country_code=country_code,
            is_primary=is_primary,
        )

        # Redirect back to customer detail page
        redirect_url = f"/admin/customers/{customer_type}/{customer_id}"
        if request.headers.get("HX-Request"):
            return HTMLResponse(content="", headers={"HX-Redirect": redirect_url})
        return RedirectResponse(url=redirect_url, status_code=303)

    except Exception as e:
        from app.web.admin import get_current_user, get_sidebar_stats
        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        return templates.TemplateResponse(
            "admin/errors/500.html",
            {"request": request, "error": str(e), "current_user": current_user, "sidebar_stats": sidebar_stats},
            status_code=500,
        )


@router.post(
    "/addresses/{address_id}/geocode",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def geocode_address(
    address_id: str,
    latitude: float = Body(...),
    longitude: float = Body(...),
    db: Session = Depends(get_db),
):
    """Update address coordinates from a geocoding selection."""
    from app.schemas.subscriber import AddressUpdate

    try:
        parsed_address_id = uuid.UUID(address_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid address id") from exc

    address = subscriber_service.addresses.update(
        db=db,
        address_id=str(parsed_address_id),
        payload=AddressUpdate(latitude=latitude, longitude=longitude),
    )
    return JSONResponse(
        {
            "success": True,
            "address_id": str(address.id),
            "latitude": address.latitude,
            "longitude": address.longitude,
        }
    )


@router.post(
    "/profile/{customer_id}/geocode-primary",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def geocode_primary_address(
    customer_id: str,
    latitude: float = Body(...),
    longitude: float = Body(...),
    db: Session = Depends(get_db),
):
    """Save coordinates to a primary address, creating one from profile address if missing."""
    from app.schemas.subscriber import AddressCreate, AddressUpdate

    try:
        parsed_customer_id = uuid.UUID(customer_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid customer id") from exc

    customer = subscriber_service.subscribers.get(db=db, subscriber_id=str(parsed_customer_id))
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    addresses = subscriber_service.addresses.list(
        db=db,
        subscriber_id=str(parsed_customer_id),
        order_by="created_at",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    primary_address = next((addr for addr in addresses if addr.is_primary), addresses[0] if addresses else None)

    created = False
    if primary_address is None:
        if not (customer.address_line1 or "").strip():
            raise HTTPException(
                status_code=400,
                detail="No address exists to geolocate. Add an address first.",
            )
        primary_address = subscriber_service.addresses.create(
            db=db,
            payload=AddressCreate(
                subscriber_id=parsed_customer_id,
                address_line1=customer.address_line1,
                address_line2=customer.address_line2,
                city=customer.city,
                region=customer.region,
                postal_code=customer.postal_code,
                country_code=customer.country_code,
                latitude=latitude,
                longitude=longitude,
                is_primary=True,
            ),
        )
        created = True

    updated = subscriber_service.addresses.update(
        db=db,
        address_id=str(primary_address.id),
        payload=AddressUpdate(latitude=latitude, longitude=longitude),
    )
    return JSONResponse(
        {
            "success": True,
            "created_address": created,
            "address_id": str(updated.id),
            "latitude": updated.latitude,
            "longitude": updated.longitude,
        }
    )


@router.delete("/addresses/{address_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:write"))])
def delete_address(
    request: Request,
    address_id: str,
    db: Session = Depends(get_db),
):
    """Delete an address."""
    try:
        web_customer_actions_service.delete_customer_address(db=db, address_id=address_id)
        # Return empty response for HTMX to remove the element
        return HTMLResponse(content="")
    except Exception as e:
        return HTMLResponse(
            content=f'<div class="text-red-500 text-sm p-2">Error: {str(e)}</div>',
            status_code=500,
        )


# ============================================================================
# Contact Management Routes
# ============================================================================

@router.post("/contacts", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:write"))])
def create_contact(
    request: Request,
    account_id: str = Form(...),
    customer_type: str = Form(...),
    customer_id: str = Form(...),
    first_name: str = Form(...),
    last_name: str = Form(...),
    role: str = Form("primary"),
    title: str | None = Form(None),
    email: str | None = Form(None),
    phone: str | None = Form(None),
    is_primary: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Create a new contact for an account."""
    try:
        web_customer_actions_service.create_customer_contact(
            db=db,
            account_id=account_id,
            first_name=first_name,
            last_name=last_name,
            role=role,
            title=title,
            email=email,
            phone=phone,
            is_primary=is_primary,
        )

        # Redirect back to customer detail page
        redirect_url = f"/admin/customers/{customer_type}/{customer_id}"
        if request.headers.get("HX-Request"):
            return HTMLResponse(content="", headers={"HX-Redirect": redirect_url})
        return RedirectResponse(url=redirect_url, status_code=303)

    except Exception as e:
        from app.web.admin import get_current_user, get_sidebar_stats
        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        return templates.TemplateResponse(
            "admin/errors/500.html",
            {"request": request, "error": str(e), "current_user": current_user, "sidebar_stats": sidebar_stats},
            status_code=500,
        )


@router.delete("/contacts/{contact_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:write"))])
def delete_contact(
    request: Request,
    contact_id: str,
    db: Session = Depends(get_db),
):
    """Delete a contact."""
    try:
        web_customer_actions_service.delete_customer_contact(db=db, contact_id=contact_id)
        # Return empty response for HTMX to remove the element
        return HTMLResponse(content="")
    except Exception as e:
        return HTMLResponse(
            content=f'<div class="text-red-500 text-sm p-2">Error: {str(e)}</div>',
            status_code=500,
        )


# ============================================================================
# Bulk Operations Routes
# ============================================================================

@router.post("/bulk/status", dependencies=[Depends(require_permission("customer:write"))])
def bulk_update_status(
    request: Request,
    data: dict = Depends(parse_json_body),
    db: Session = Depends(get_db),
):
    """Bulk update customer status (activate/deactivate)."""
    try:
        customer_ids = data.get("customer_ids", [])
        new_status = data.get("status")

        if not customer_ids or not new_status:
            raise HTTPException(status_code=400, detail="customer_ids and status are required")

        if new_status not in ("active", "inactive"):
            raise HTTPException(status_code=400, detail="status must be 'active' or 'inactive'")

        is_active = new_status == "active"
        return web_customer_actions_service.bulk_update_customer_status(
            db=db,
            customer_ids=customer_ids,
            is_active=is_active,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/bulk/delete", dependencies=[Depends(require_permission("customer:delete"))])
def bulk_delete_customers(
    request: Request,
    data: dict = Depends(parse_json_body),
    db: Session = Depends(get_db),
):
    """Bulk delete customers (only inactive customers without subscribers)."""
    try:
        customer_ids = data.get("customer_ids", [])

        if not customer_ids:
            raise HTTPException(status_code=400, detail="customer_ids is required")
        return web_customer_actions_service.bulk_delete_customers(
            db=db,
            customer_ids=customer_ids,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/export", dependencies=[Depends(require_permission("customer:read"))])
def export_customers(
    request: Request,
    export: str = Query("csv"),
    ids: str = Query("all"),
    search: str | None = None,
    customer_type: str | None = None,
    db: Session = Depends(get_db),
):
    """Export customers to CSV or Excel format."""
    content, filename = web_customer_actions_service.export_customers_csv(
        db=db,
        ids=ids,
        search=search,
        customer_type=customer_type,
    )

    return Response(
        content=content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
        },
    )
