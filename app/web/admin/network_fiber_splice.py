"""Admin network fiber splice web routes."""

from typing import cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.network import (
    FiberSplice,
    FiberSpliceClosure,
    FiberSpliceTray,
    FiberStrand,
)
from app.services import (
    web_network_splice_closures as web_network_splice_closures_service,
)
from app.services import web_network_strands as web_network_strands_service
from app.services.audit_helpers import (
    build_audit_activities,
    diff_dicts,
    log_audit_event,
    model_to_dict,
)
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])


def _base_context(
    request: Request, db: Session, active_page: str, active_menu: str = "network"
) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get("/fiber-strands", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def fiber_strands_list(request: Request, db: Session = Depends(get_db)):
    page_data = web_network_strands_service.list_page_data(db)
    context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/fiber/strands.html", context)


@router.get("/fiber-strands/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def fiber_strand_new(request: Request, db: Session = Depends(get_db)):
    form_context = web_network_strands_service.build_form_context(
        strand=None,
        action_url="/admin/network/fiber-strands",
    )
    context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/strand-form.html", context)


@router.post("/fiber-strands", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def fiber_strand_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    values = web_network_strands_service.parse_form_values(form)
    _, error = web_network_strands_service.validate_form_values(values)

    if error:
        strand_data = web_network_strands_service.strand_form_data(values)
        form_context = web_network_strands_service.build_form_context(
            strand=strand_data,
            action_url="/admin/network/fiber-strands",
            error=error,
        )
        context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/strand-form.html", context)

    try:
        strand = web_network_strands_service.create_strand(db, values)
        from app.web.admin import get_current_user

        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="fiber_strand",
            entity_id=str(strand.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={
                "cable_name": strand.cable_name,
                "strand_number": strand.strand_number,
            },
        )
        return RedirectResponse("/admin/network/fiber-strands", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except ValueError as exc:
        error = str(exc)
    except Exception as exc:
        error = str(exc)

    strand_data = web_network_strands_service.strand_form_data(values)
    form_context = web_network_strands_service.build_form_context(
        strand=strand_data,
        action_url="/admin/network/fiber-strands",
        error=error or "Please correct the highlighted fields.",
    )
    context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/strand-form.html", context)


@router.get("/fiber-strands/{strand_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def fiber_strand_edit(request: Request, strand_id: str, db: Session = Depends(get_db)):
    strand = web_network_strands_service.get_strand(db, strand_id)
    form_context = web_network_strands_service.build_form_context(
        strand=strand,
        action_url=f"/admin/network/fiber-strands/{strand_id}",
    )
    context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/strand-form.html", context)


@router.post("/fiber-strands/{strand_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def fiber_strand_update(request: Request, strand_id: str, db: Session = Depends(get_db)):
    strand = web_network_strands_service.get_strand(db, strand_id)

    form = parse_form_data_sync(request)
    values = web_network_strands_service.parse_form_values(form)
    _, error = web_network_strands_service.validate_form_values(values)

    if error:
        strand_data = web_network_strands_service.strand_form_data(
            values, strand_id=str(strand.id)
        )
        form_context = web_network_strands_service.build_form_context(
            strand=strand_data,
            action_url=f"/admin/network/fiber-strands/{strand_id}",
            error=error,
        )
        context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/strand-form.html", context)

    try:
        before_snapshot = model_to_dict(strand)
        updated_strand = web_network_strands_service.update_strand(db, strand_id, values)
        after_snapshot = model_to_dict(updated_strand)
        changes = diff_dicts(before_snapshot, after_snapshot)
        metadata = {"changes": changes} if changes else None
        from app.web.admin import get_current_user

        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="fiber_strand",
            entity_id=str(updated_strand.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata,
        )
        return RedirectResponse("/admin/network/fiber-strands", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except ValueError as exc:
        error = str(exc)
    except Exception as exc:
        error = str(exc)

    strand_data = web_network_strands_service.strand_form_data(values, strand_id=str(strand.id))
    form_context = web_network_strands_service.build_form_context(
        strand=strand_data,
        action_url=f"/admin/network/fiber-strands/{strand_id}",
        error=error or "Please correct the highlighted fields.",
    )
    context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/strand-form.html", context)


@router.get("/splice-closures", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def splice_closures_list(request: Request, db: Session = Depends(get_db)):
    page_data = web_network_splice_closures_service.list_page_data(db)
    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/fiber/splice-closures.html", context)


@router.get("/splice-closures/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def splice_closure_new(request: Request, db: Session = Depends(get_db)):
    form_context = web_network_splice_closures_service.build_form_context(
        closure=None,
        action_url="/admin/network/splice-closures",
    )
    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splice-closure-form.html", context)


@router.post("/splice-closures", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def splice_closure_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    values = web_network_splice_closures_service.parse_form_values(form)
    error = web_network_splice_closures_service.validate_name(values)
    if error:
        form_context = web_network_splice_closures_service.build_form_context(
            closure=None,
            action_url="/admin/network/splice-closures",
            error=error,
        )
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/splice-closure-form.html", context)

    closure = web_network_splice_closures_service.create_closure(db, values)
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="splice_closure",
        entity_id=str(closure.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": closure.name},
    )

    return RedirectResponse(f"/admin/network/splice-closures/{closure.id}", status_code=303)


@router.get("/splice-closures/{closure_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def splice_closure_edit(request: Request, closure_id: str, db: Session = Depends(get_db)):
    closure = web_network_splice_closures_service.get_closure(db, closure_id)
    if not closure:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )
    form_context = web_network_splice_closures_service.build_form_context(
        closure=closure,
        action_url=f"/admin/network/splice-closures/{closure.id}",
    )
    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splice-closure-form.html", context)


@router.post("/splice-closures/{closure_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def splice_closure_update(request: Request, closure_id: str, db: Session = Depends(get_db)):
    closure = web_network_splice_closures_service.get_closure(db, closure_id)
    if not closure:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )

    before_snapshot = model_to_dict(closure)
    form = parse_form_data_sync(request)
    values = web_network_splice_closures_service.parse_form_values(form)
    error = web_network_splice_closures_service.validate_name(values)
    if error:
        form_context = web_network_splice_closures_service.build_form_context(
            closure=closure,
            action_url=f"/admin/network/splice-closures/{closure.id}",
            error=error,
        )
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/splice-closure-form.html", context)

    web_network_splice_closures_service.commit_closure_update(db, closure, values)

    after_snapshot = model_to_dict(closure)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata = {"changes": changes} if changes else None
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="splice_closure",
        entity_id=str(closure.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata,
    )

    return RedirectResponse(f"/admin/network/splice-closures/{closure.id}", status_code=303)


@router.get("/splice-closures/{closure_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def splice_closure_detail(request: Request, closure_id: str, db: Session = Depends(get_db)):
    page_data = web_network_splice_closures_service.detail_page_data(db, closure_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update(page_data)
    context["activities"] = build_audit_activities(db, "splice_closure", str(closure_id), limit=10)
    return templates.TemplateResponse("admin/network/fiber/splice-closure-detail.html", context)


@router.get("/splice-closures/{closure_id}/trays/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def splice_tray_new(request: Request, closure_id: str, db: Session = Depends(get_db)):
    closure = web_network_splice_closures_service.get_closure(db, closure_id)
    if not closure:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )

    form_context = web_network_splice_closures_service.build_tray_form_context(
        closure=closure,
        tray=None,
        action_url=f"/admin/network/splice-closures/{closure_id}/trays",
    )
    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splice-tray-form.html", context)


@router.get("/splice-closures/{closure_id}/trays", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def splice_tray_redirect(closure_id: str):
    return RedirectResponse(f"/admin/network/splice-closures/{closure_id}", status_code=303)


@router.get("/splice-closures/{closure_id}/trays/{tray_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def splice_tray_edit(
    request: Request,
    closure_id: str,
    tray_id: str,
    db: Session = Depends(get_db),
):
    closure = web_network_splice_closures_service.get_closure(db, closure_id)
    tray = web_network_splice_closures_service.get_tray(db, closure_id, tray_id)
    if not closure or not tray:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Tray not found"},
            status_code=404,
        )

    form_context = web_network_splice_closures_service.build_tray_form_context(
        closure=closure,
        tray=tray,
        action_url=f"/admin/network/splice-closures/{closure_id}/trays/{tray_id}",
    )
    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splice-tray-form.html", context)


@router.post("/splice-closures/{closure_id}/trays", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def splice_tray_create(request: Request, closure_id: str, db: Session = Depends(get_db)):
    closure = web_network_splice_closures_service.get_closure(db, closure_id)
    if not closure:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )

    form = parse_form_data_sync(request)
    values = web_network_splice_closures_service.parse_tray_form_values(form)
    _, error = web_network_splice_closures_service.validate_tray_form_values(values)
    if error:
        tray_data = web_network_splice_closures_service.tray_form_data(values)
        form_context = web_network_splice_closures_service.build_tray_form_context(
            closure=closure,
            tray=tray_data,
            action_url=f"/admin/network/splice-closures/{closure_id}/trays",
            error=error,
        )
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/splice-tray-form.html", context)

    try:
        tray = web_network_splice_closures_service.create_tray(db, str(closure.id), values)
        from app.web.admin import get_current_user

        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="tray_created",
            entity_type="splice_closure",
            entity_id=str(closure.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"tray_number": tray.tray_number, "tray_name": tray.name},
        )
        return RedirectResponse(f"/admin/network/splice-closures/{closure.id}", status_code=303)
    except Exception as exc:
        tray_data = web_network_splice_closures_service.tray_form_data(values)
        form_context = web_network_splice_closures_service.build_tray_form_context(
            closure=closure,
            tray=tray_data,
            action_url=f"/admin/network/splice-closures/{closure_id}/trays",
            error=str(exc),
        )
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/splice-tray-form.html", context)


@router.post("/splice-closures/{closure_id}/trays/{tray_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def splice_tray_update(
    request: Request,
    closure_id: str,
    tray_id: str,
    db: Session = Depends(get_db),
):
    closure = web_network_splice_closures_service.get_closure(db, closure_id)
    tray = web_network_splice_closures_service.get_tray(db, closure_id, tray_id)
    if not closure or not tray:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Tray not found"},
            status_code=404,
        )

    form = parse_form_data_sync(request)
    values = web_network_splice_closures_service.parse_tray_form_values(form)
    _, error = web_network_splice_closures_service.validate_tray_form_values(values)
    if error:
        tray_data = web_network_splice_closures_service.tray_form_data(values, tray_id=str(tray.id))
        form_context = web_network_splice_closures_service.build_tray_form_context(
            closure=closure,
            tray=tray_data,
            action_url=f"/admin/network/splice-closures/{closure_id}/trays/{tray_id}",
            error=error,
        )
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/splice-tray-form.html", context)

    try:
        web_network_splice_closures_service.commit_tray_update(db, tray, values)
        from app.web.admin import get_current_user

        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="tray_updated",
            entity_type="splice_closure",
            entity_id=str(closure.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"tray_number": tray.tray_number, "tray_name": tray.name},
        )
        return RedirectResponse(f"/admin/network/splice-closures/{closure.id}", status_code=303)
    except Exception as exc:
        tray_data = web_network_splice_closures_service.tray_form_data(values, tray_id=str(tray.id))
        form_context = web_network_splice_closures_service.build_tray_form_context(
            closure=closure,
            tray=tray_data,
            action_url=f"/admin/network/splice-closures/{closure_id}/trays/{tray_id}",
            error=str(exc),
        )
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/splice-tray-form.html", context)


@router.get("/splice-closures/{closure_id}/splices/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def splice_new(request: Request, closure_id: str, db: Session = Depends(get_db)):
    dependencies = web_network_splice_closures_service.splice_form_dependencies(db, closure_id)
    if not dependencies:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )

    closure = cast(FiberSpliceClosure, dependencies["closure"])
    trays = cast(list[FiberSpliceTray], dependencies["trays"])
    strands = cast(list[FiberStrand], dependencies["strands"])
    form_context = web_network_splice_closures_service.build_splice_form_context(
        closure=closure,
        trays=trays,
        strands=strands,
        splice=None,
        action_url=f"/admin/network/splice-closures/{closure_id}/splices",
    )
    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splice-form.html", context)


@router.post("/splice-closures/{closure_id}/splices", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def splice_create(request: Request, closure_id: str, db: Session = Depends(get_db)):
    dependencies = web_network_splice_closures_service.splice_form_dependencies(db, closure_id)
    if not dependencies:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )
    closure = cast(FiberSpliceClosure, dependencies["closure"])
    trays = cast(list[FiberSpliceTray], dependencies["trays"])
    strands = cast(list[FiberStrand], dependencies["strands"])

    form = parse_form_data_sync(request)
    values = web_network_splice_closures_service.parse_splice_form_values(form)
    _, error = web_network_splice_closures_service.validate_splice_form_values(values)

    if error:
        splice_data = web_network_splice_closures_service.splice_form_data(values)
        form_context = web_network_splice_closures_service.build_splice_form_context(
            closure=closure,
            trays=trays,
            strands=strands,
            splice=splice_data,
            action_url=f"/admin/network/splice-closures/{closure_id}/splices",
            error=error,
        )
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/splice-form.html", context)

    try:
        splice = cast(
            FiberSplice,
            web_network_splice_closures_service.create_splice(db, str(closure.id), values),
        )
        from app.web.admin import get_current_user

        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="splice_created",
            entity_type="splice_closure",
            entity_id=str(closure.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={
                "from_strand_id": str(splice.from_strand_id) if splice.from_strand_id else None,
                "to_strand_id": str(splice.to_strand_id) if splice.to_strand_id else None,
            },
        )
        return RedirectResponse(f"/admin/network/splice-closures/{closure.id}", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except ValueError as exc:
        error = str(exc)
    except Exception as exc:
        error = str(exc)

    splice_data = web_network_splice_closures_service.splice_form_data(values)
    form_context = web_network_splice_closures_service.build_splice_form_context(
        closure=closure,
        trays=trays,
        strands=strands,
        splice=splice_data,
        action_url=f"/admin/network/splice-closures/{closure_id}/splices",
        error=error or "Please correct the highlighted fields.",
    )
    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splice-form.html", context)


@router.get("/splice-closures/{closure_id}/splices/{splice_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:read"))])
def splice_edit(
    request: Request,
    closure_id: str,
    splice_id: str,
    db: Session = Depends(get_db),
):
    dependencies = web_network_splice_closures_service.splice_form_dependencies(db, closure_id)
    splice = web_network_splice_closures_service.get_splice(db, closure_id, splice_id)
    if not dependencies or not splice:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Fiber splice not found"},
            status_code=404,
        )
    closure = cast(FiberSpliceClosure, dependencies["closure"])
    trays = cast(list[FiberSpliceTray], dependencies["trays"])
    strands = cast(list[FiberStrand], dependencies["strands"])
    form_context = web_network_splice_closures_service.build_splice_form_context(
        closure=closure,
        trays=trays,
        strands=strands,
        splice=cast(FiberSplice, splice),
        action_url=f"/admin/network/splice-closures/{closure_id}/splices/{splice_id}",
    )
    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splice-form.html", context)


@router.post("/splice-closures/{closure_id}/splices/{splice_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("network:write"))])
def splice_update(
    request: Request,
    closure_id: str,
    splice_id: str,
    db: Session = Depends(get_db),
):
    dependencies = web_network_splice_closures_service.splice_form_dependencies(db, closure_id)
    splice = web_network_splice_closures_service.get_splice(db, closure_id, splice_id)
    if not dependencies or not splice:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Fiber splice not found"},
            status_code=404,
        )
    closure = cast(FiberSpliceClosure, dependencies["closure"])
    trays = cast(list[FiberSpliceTray], dependencies["trays"])
    strands = cast(list[FiberStrand], dependencies["strands"])

    form = parse_form_data_sync(request)
    values = web_network_splice_closures_service.parse_splice_form_values(form)
    _, error = web_network_splice_closures_service.validate_splice_form_values(values)

    if error:
        splice_data = web_network_splice_closures_service.splice_form_data(
            values, splice_id=str(splice.id)
        )
        form_context = web_network_splice_closures_service.build_splice_form_context(
            closure=closure,
            trays=trays,
            strands=strands,
            splice=splice_data,
            action_url=f"/admin/network/splice-closures/{closure_id}/splices/{splice_id}",
            error=error,
        )
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update(form_context)
        return templates.TemplateResponse("admin/network/fiber/splice-form.html", context)

    try:
        updated_splice = cast(
            FiberSplice,
            web_network_splice_closures_service.update_splice(db, splice_id, values),
        )
        from app.web.admin import get_current_user

        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="splice_updated",
            entity_type="splice_closure",
            entity_id=str(closure.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={
                "from_strand_id": str(updated_splice.from_strand_id)
                if updated_splice.from_strand_id
                else None,
                "to_strand_id": str(updated_splice.to_strand_id)
                if updated_splice.to_strand_id
                else None,
            },
        )
        return RedirectResponse(f"/admin/network/splice-closures/{closure.id}", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except ValueError as exc:
        error = str(exc)
    except Exception as exc:
        error = str(exc)

    splice_data = web_network_splice_closures_service.splice_form_data(
        values, splice_id=str(splice.id)
    )
    form_context = web_network_splice_closures_service.build_splice_form_context(
        closure=closure,
        trays=trays,
        strands=strands,
        splice=splice_data,
        action_url=f"/admin/network/splice-closures/{closure_id}/splices/{splice_id}",
        error=error or "Please correct the highlighted fields.",
    )
    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splice-form.html", context)
