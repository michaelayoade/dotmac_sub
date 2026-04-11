"""Admin reseller portal web routes."""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.db import get_db
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
    context = _base_context(request, db, active_page="resellers")
    context.update(reseller_svc.list_page_context(db, page=page, per_page=per_page))
    return templates.TemplateResponse("admin/resellers/index.html", context)


@router.get("/new", response_class=HTMLResponse)
def reseller_new(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    context = _base_context(request, db, active_page="resellers")
    context.update(reseller_svc.new_form_context(db))
    return templates.TemplateResponse("admin/resellers/reseller_form.html", context)


@router.get("/{reseller_id}/edit", response_class=HTMLResponse)
def reseller_edit(
    reseller_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    context = _base_context(request, db, active_page="resellers")
    context.update(reseller_svc.edit_form_context(db, reseller_id=reseller_id))
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
    payload = reseller_svc.parse_reseller_payload(form)
    user_payload = reseller_svc.parse_create_user_payload(form)
    user_error = reseller_svc.validate_create_user_payload(user_payload)
    if user_error:
        context = _base_context(request, db, active_page="resellers")
        context.update(
            reseller_svc.create_form_error_context(
                db,
                payload=payload,
                error=user_error,
            )
        )
        return templates.TemplateResponse(
            "admin/resellers/reseller_form.html", context, status_code=400
        )
    try:
        reseller_svc.create_reseller_from_form(db, form)
    except Exception as exc:
        context = _base_context(request, db, active_page="resellers")
        context.update(
            reseller_svc.create_form_error_context(
                db,
                payload=payload,
                error=str(exc) or "Unable to create reseller.",
            )
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
    payload = reseller_svc.parse_reseller_payload(form)
    try:
        reseller_svc.update_reseller_from_form(db, reseller_id=reseller_id, form=form)
    except Exception as exc:
        context = _base_context(request, db, active_page="resellers")
        context.update(
            reseller_svc.update_form_error_context(
                reseller_id=reseller_id,
                payload=payload,
                error=str(exc) or "Unable to update reseller.",
            )
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
