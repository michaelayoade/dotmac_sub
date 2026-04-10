"""Admin reseller portal web routes."""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.db import get_db
from app.schemas.subscriber import ResellerCreate, ResellerUpdate
from app.services import rbac as rbac_service
from app.services import subscriber as subscriber_service
from app.services import web_admin_resellers as reseller_svc
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/resellers", tags=["web-admin-resellers"])


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value if isinstance(value, str) else default


def _form_int(form: FormData, key: str, default: int) -> int:
    raw = _form_str(form, key, str(default)).strip()
    try:
        return int(raw)
    except Exception:
        return default


def _base_context(request: Request, db: Session, active_page: str) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "resellers",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:read"))],
)
def resellers_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=200),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    total = subscriber_service.resellers.count(db=db, is_active=True)
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page
    resellers = subscriber_service.resellers.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=per_page,
        offset=offset,
    )
    reseller_subscriber_counts = reseller_svc.count_subscribers_by_reseller_ids(
        db,
        [str(item.id) for item in resellers],
    )
    context = _base_context(request, db, active_page="resellers")
    context.update(
        {
            "resellers": resellers,
            "reseller_subscriber_counts": reseller_subscriber_counts,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        }
    )
    return templates.TemplateResponse("admin/resellers/index.html", context)


@router.get("/new", response_class=HTMLResponse)
def reseller_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = _base_context(request, db, active_page="resellers")
    roles = rbac_service.roles.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    context.update({"reseller": None, "action_url": "/admin/resellers", "roles": roles})
    return templates.TemplateResponse("admin/resellers/reseller_form.html", context)


@router.get("/{reseller_id}/edit", response_class=HTMLResponse)
def reseller_edit(
    reseller_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    reseller = subscriber_service.resellers.get(db=db, reseller_id=reseller_id)
    context = _base_context(request, db, active_page="resellers")
    context.update(
        {
            "reseller": reseller,
            "action_url": f"/admin/resellers/{reseller.id}",
        }
    )
    return templates.TemplateResponse("admin/resellers/reseller_form.html", context)


@router.post(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("customer:write"))],
)
def reseller_create(
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    create_user = bool(form.get("create_user"))
    payload = {
        "name": _form_str(form, "name").strip(),
        "code": _form_str(form, "code").strip() or None,
        "contact_email": _form_str(form, "contact_email").strip() or None,
        "contact_phone": _form_str(form, "contact_phone").strip() or None,
        "notes": _form_str(form, "notes").strip() or None,
        "is_active": bool(form.get("is_active")),
    }
    user_payload: dict[str, str | None] | None = None
    if create_user:
        user_payload = {
            "first_name": _form_str(form, "user_first_name").strip(),
            "last_name": _form_str(form, "user_last_name").strip(),
            "email": _form_str(form, "user_email").strip(),
            "username": (_form_str(form, "user_email").strip() or None),
            "role": _form_str(form, "user_role").strip() or None,
        }
        missing = [
            key
            for key, value in user_payload.items()
            if key not in {"role", "username"} and not value
        ]
        if missing:
            context = _base_context(request, db, active_page="resellers")
            roles = rbac_service.roles.list(
                db=db,
                is_active=True,
                order_by="name",
                order_dir="asc",
                limit=500,
                offset=0,
            )
            context.update(
                {
                    "reseller": payload,
                    "action_url": "/admin/resellers",
                    "roles": roles,
                    "error": "Provide first name, last name, and email to create a reseller portal user.",
                }
            )
            return templates.TemplateResponse(
                "admin/resellers/reseller_form.html", context, status_code=400
            )
    try:
        data = ResellerCreate.model_validate(payload)
    except ValidationError as exc:
        context = _base_context(request, db, active_page="resellers")
        roles = rbac_service.roles.list(
            db=db,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=500,
            offset=0,
        )
        context.update(
            {
                "reseller": payload,
                "action_url": "/admin/resellers",
                "roles": roles,
                "error": exc.errors()[0].get("msg", "Invalid reseller details."),
            }
        )
        return templates.TemplateResponse(
            "admin/resellers/reseller_form.html", context, status_code=400
        )
    reseller = subscriber_service.resellers.create(db=db, payload=data)
    if user_payload:
        try:
            reseller_svc.create_reseller_with_user(
                db, reseller=reseller, user_payload=user_payload
            )
        except Exception as exc:
            context = _base_context(request, db, active_page="resellers")
            roles = rbac_service.roles.list(
                db=db,
                is_active=True,
                order_by="name",
                order_dir="asc",
                limit=500,
                offset=0,
            )
            context.update(
                {
                    "reseller": payload,
                    "action_url": "/admin/resellers",
                    "roles": roles,
                    "error": str(exc) or "Unable to create login user.",
                }
            )
            return templates.TemplateResponse(
                "admin/resellers/reseller_form.html", context, status_code=400
            )
    return RedirectResponse(url="/admin/resellers", status_code=303)


@router.post("/{reseller_id}", response_class=HTMLResponse)
def reseller_update(
    reseller_id: str,
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    payload = {
        "name": _form_str(form, "name").strip(),
        "code": _form_str(form, "code").strip() or None,
        "contact_email": _form_str(form, "contact_email").strip() or None,
        "contact_phone": _form_str(form, "contact_phone").strip() or None,
        "notes": _form_str(form, "notes").strip() or None,
        "is_active": bool(form.get("is_active")),
    }
    try:
        data = ResellerUpdate.model_validate(payload)
    except ValidationError as exc:
        context = _base_context(request, db, active_page="resellers")
        payload.update({"id": reseller_id})
        context.update(
            {
                "reseller": payload,
                "action_url": f"/admin/resellers/{reseller_id}",
                "error": exc.errors()[0].get("msg", "Invalid reseller details."),
            }
        )
        return templates.TemplateResponse(
            "admin/resellers/reseller_form.html", context, status_code=400
        )
    try:
        subscriber_service.resellers.update(
            db=db, reseller_id=reseller_id, payload=data
        )
    except Exception as exc:
        context = _base_context(request, db, active_page="resellers")
        payload.update({"id": reseller_id})
        context.update(
            {
                "reseller": payload,
                "action_url": f"/admin/resellers/{reseller_id}",
                "error": str(exc) or "Unable to update reseller.",
            }
        )
        return templates.TemplateResponse(
            "admin/resellers/reseller_form.html", context, status_code=400
        )
    return RedirectResponse(url="/admin/resellers", status_code=303)


@router.get("/{reseller_id}", response_class=HTMLResponse)
def reseller_detail(
    reseller_id: str,
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=200),
    db: Session = Depends(get_db),
):
    detail = reseller_svc.get_reseller_detail_context(
        db,
        reseller_id,
        page=page,
        per_page=per_page,
    )
    if not detail:
        return RedirectResponse(url="/admin/resellers", status_code=303)
    context = _base_context(request, db, active_page="resellers")
    context.update(detail)
    return templates.TemplateResponse("admin/resellers/detail.html", context)


@router.post("/{reseller_id}/users/link", response_class=HTMLResponse)
def reseller_user_link(
    reseller_id: str,
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    page = _form_int(form, "page", 1)
    per_page = _form_int(form, "per_page", 50)
    subscriber_id = (
        _form_str(form, "subscriber_id").strip() or _form_str(form, "person_id").strip()
    )
    if subscriber_id:
        try:
            reseller_svc.link_existing_subscriber_to_reseller(
                db, reseller_id=reseller_id, subscriber_id=subscriber_id
            )
        except Exception as exc:
            detail = reseller_svc.get_reseller_detail_context(
                db,
                reseller_id,
                page=page,
                per_page=per_page,
            )
            context = _base_context(request, db, active_page="resellers")
            context.update(detail or {})
            context["error"] = str(exc) or "Unable to link subscriber."
            return templates.TemplateResponse(
                "admin/resellers/detail.html", context, status_code=400
            )
    return RedirectResponse(
        url=f"/admin/resellers/{reseller_id}?page={page}&per_page={per_page}",
        status_code=303,
    )


@router.post("/{reseller_id}/users/create", response_class=HTMLResponse)
def reseller_user_create(
    reseller_id: str,
    request: Request,
    form: FormData = Depends(parse_form_data),
    db: Session = Depends(get_db),
):
    page = _form_int(form, "page", 1)
    per_page = _form_int(form, "per_page", 50)
    fields = {
        "first_name": _form_str(form, "first_name").strip(),
        "last_name": _form_str(form, "last_name").strip(),
        "email": _form_str(form, "email").strip(),
        "username": _form_str(form, "email").strip(),
    }
    if not all([fields["first_name"], fields["last_name"], fields["email"]]):
        detail = reseller_svc.get_reseller_detail_context(
            db,
            reseller_id,
            page=page,
            per_page=per_page,
        )
        context = _base_context(request, db, active_page="resellers")
        context.update(detail or {})
        context["error"] = "First name, last name, and email are required."
        return templates.TemplateResponse(
            "admin/resellers/detail.html", context, status_code=400
        )
    try:
        reseller_svc.create_and_link_reseller_user(
            db,
            reseller_id=reseller_id,
            first_name=fields["first_name"],
            last_name=fields["last_name"],
            email=fields["email"],
            username=fields["username"],
        )
    except Exception as exc:
        detail = reseller_svc.get_reseller_detail_context(
            db,
            reseller_id,
            page=page,
            per_page=per_page,
        )
        context = _base_context(request, db, active_page="resellers")
        context.update(detail or {})
        context["error"] = str(exc) or "Unable to create reseller user."
        return templates.TemplateResponse(
            "admin/resellers/detail.html", context, status_code=400
        )
    return RedirectResponse(
        url=f"/admin/resellers/{reseller_id}?page={page}&per_page={per_page}",
        status_code=303,
    )
