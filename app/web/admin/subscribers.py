"""Admin subscriber management web routes."""

import json
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import subscriber as subscriber_service
from app.services import web_subscriber_actions as web_subscriber_actions_service
from app.services.audit_helpers import build_changes_metadata, log_audit_event
from app.services.web_subscriber_details import (
    build_subscriber_detail_page_context,
)
from app.services.web_subscriber_forms import (
    build_subscriber_update_form_values,
    load_subscriber_form_options,
    resolve_new_form_prefill,
)
from app.web.request_parsing import parse_json_body

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/subscribers", tags=["web-admin-subscribers"])


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


@router.get("", response_class=HTMLResponse)
def subscribers_list(
    request: Request,
    search: str | None = None,
    subscriber_type: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List all subscribers with search and filtering."""
    offset = (page - 1) * per_page

    subscribers = subscriber_service.subscribers.list(
        db=db,
        subscriber_type=subscriber_type if subscriber_type else None,
        organization_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )

    total = subscriber_service.subscribers.count(
        db=db,
        subscriber_type=subscriber_type if subscriber_type else None,
    )
    total_pages = (total + per_page - 1) // per_page

    # Check if this is an HTMX request for table body only
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "admin/subscribers/_table.html",
            {
                "request": request,
                "subscribers": subscribers,
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": total_pages,
                "search": search,
            },
        )

    # Get sidebar stats and current user
    from app.web.admin import get_current_user, get_sidebar_stats
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    # Get stats for dashboard cards
    stats = subscriber_service.subscribers.count_stats(db)

    return templates.TemplateResponse(
        "admin/subscribers/index.html",
        {
            "request": request,
            "subscribers": subscribers,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "search": search,
            "subscriber_type": subscriber_type,
            "status": status,
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
            "stats": stats,
            "active_page": "subscribers",
        },
    )


@router.get("/create", response_class=HTMLResponse)
def subscribers_create_redirect():
    return RedirectResponse(url="/admin/subscribers/new", status_code=303)


# Note: /new routes must be defined BEFORE /{subscriber_id} to avoid path matching issues
@router.get("/new", response_class=HTMLResponse)
def subscriber_new(request: Request, db: Session = Depends(get_db)):
    """New subscriber form."""
    from app.web.admin import get_current_user, get_sidebar_stats

    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)
    subscriber_id = request.query_params.get("subscriber_id", "").strip() or None
    organization_id = request.query_params.get("organization_id", "").strip() or None
    prefill_ref, prefill_label = resolve_new_form_prefill(
        db,
        subscriber_id=subscriber_id,
        organization_id=organization_id,
    )

    people, organizations = load_subscriber_form_options(db)

    return templates.TemplateResponse(
        "admin/subscribers/form.html",
        {
            "request": request,
            "subscriber": None,
            "action": "create",
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
            "people": people,
            "organizations": organizations,
            "prefill_ref": prefill_ref,
            "prefill_label": prefill_label,
        },
    )


@router.post("/new", response_class=HTMLResponse)
def subscriber_create(
    request: Request,
    customer_ref: str | None = Form(None),
    customer_search: str | None = Form(None),
    subscriber_type: str | None = Form(None),
    person_id: str | None = Form(None),
    organization_id: str | None = Form(None),
    subscriber_number: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),
    create_user: str | None = Form(None),
    user_username: str | None = Form(None),
    user_password: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Create a new subscriber."""
    try:
        subscriber = web_subscriber_actions_service.create_subscriber_from_form(
            db=db,
            customer_ref=customer_ref,
            customer_search=customer_search,
            subscriber_type=subscriber_type,
            person_id=person_id,
            organization_id=organization_id,
            subscriber_number=subscriber_number,
            notes=notes,
            is_active=is_active,
            create_user=create_user,
            user_username=user_username,
            user_password=user_password,
        )
        return RedirectResponse(
            url=f"/admin/subscribers/{subscriber.id}",
            status_code=303,
        )
    except HTTPException:
        raise
    except Exception as e:
        from app.web.admin import get_current_user, get_sidebar_stats

        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        people, organizations = load_subscriber_form_options(db)

        return templates.TemplateResponse(
            "admin/subscribers/form.html",
            {
                "request": request,
                "subscriber": None,
                "action": "create",
                "error": str(e),
                "form": web_subscriber_actions_service.build_subscriber_create_form_values(
                    customer_ref=customer_ref,
                    customer_search=customer_search,
                    subscriber_type=subscriber_type,
                    person_id=person_id,
                    organization_id=organization_id,
                    subscriber_number=subscriber_number,
                    notes=notes,
                    is_active=is_active,
                    create_user=create_user,
                    user_username=user_username,
                ),
                "current_user": current_user,
                "sidebar_stats": sidebar_stats,
                "people": people,
                "organizations": organizations,
            },
            status_code=400,
        )


@router.get("/{subscriber_id}", response_class=HTMLResponse)
def subscriber_detail(
    request: Request,
    subscriber_id: UUID,
    db: Session = Depends(get_db),
):
    """View subscriber details."""
    try:
        page_data = build_subscriber_detail_page_context(
            db=db,
            subscriber_id=subscriber_id,
        )
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Subscriber not found"},
            status_code=404,
        )

    # Get sidebar stats and current user
    from app.web.admin import get_current_user, get_sidebar_stats
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/subscribers/detail.html",
        {
            "request": request,
            **page_data,
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.post("/{subscriber_id}/deactivate", response_class=HTMLResponse)
def subscriber_deactivate(
    request: Request,
    subscriber_id: UUID,
    db: Session = Depends(get_db),
):
    """Deactivate a subscriber before deletion."""
    before, after = web_subscriber_actions_service.deactivate_subscriber(
        db=db,
        subscriber_id=subscriber_id,
    )
    metadata = build_changes_metadata(before, after)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="subscriber",
        entity_id=str(subscriber_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata,
    )
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    return RedirectResponse(url=f"/admin/subscribers/{subscriber_id}", status_code=303)


@router.get("/{subscriber_id}/suspend", response_class=HTMLResponse)
def subscriber_suspend(request: Request, subscriber_id: str, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
    if not subscriber:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Subscriber not found"},
            status_code=404,
        )

    accounts = [subscriber]

    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)
    return templates.TemplateResponse(
        "admin/subscribers/suspend.html",
        {
            "request": request,
            "subscriber": subscriber,
            "accounts": accounts,
            "active_page": "subscribers",
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.get("/{subscriber_id}/edit", response_class=HTMLResponse)
def subscriber_edit(
    request: Request,
    subscriber_id: UUID,
    db: Session = Depends(get_db),
):
    """Edit subscriber form."""
    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=str(subscriber_id))
    if not subscriber:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Subscriber not found"},
            status_code=404,
        )

    from app.web.admin import get_current_user, get_sidebar_stats

    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    people, organizations = load_subscriber_form_options(db)

    return templates.TemplateResponse(
        "admin/subscribers/form.html",
        {
            "request": request,
            "subscriber": subscriber,
            "action": "edit",
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
            "people": people,
            "organizations": organizations,
        },
    )


@router.post("/{subscriber_id}/edit", response_class=HTMLResponse)
def subscriber_update(
    request: Request,
    subscriber_id: UUID,
    customer_ref: str | None = Form(None),
    customer_search: str | None = Form(None),
    subscriber_type: str | None = Form(None),
    person_id: str | None = Form(None),
    organization_id: str | None = Form(None),
    subscriber_number: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),  # Checkbox sends "true" or nothing
    db: Session = Depends(get_db),
):
    """Update a subscriber."""
    try:
        _, before, after = web_subscriber_actions_service.update_subscriber_from_form(
            db=db,
            subscriber_id=subscriber_id,
            customer_ref=customer_ref,
            customer_search=customer_search,
            subscriber_type=subscriber_type,
            person_id=person_id,
            organization_id=organization_id,
            subscriber_number=subscriber_number,
            notes=notes,
            is_active=is_active,
        )
        metadata = build_changes_metadata(before, after)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="subscriber",
            entity_id=str(subscriber_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata,
        )
        return RedirectResponse(
            url=f"/admin/subscribers/{subscriber_id}",
            status_code=303,
        )
    except Exception as e:
        from app.web.admin import get_current_user, get_sidebar_stats

        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=str(subscriber_id))

        people, organizations = load_subscriber_form_options(db)

        return templates.TemplateResponse(
            "admin/subscribers/form.html",
            {
                "request": request,
                "subscriber": subscriber,
                "action": "edit",
                "error": str(e),
                "form": build_subscriber_update_form_values(
                    customer_ref=customer_ref,
                    customer_search=customer_search,
                    subscriber_type=subscriber_type,
                    person_id=person_id,
                    organization_id=organization_id,
                    subscriber_number=subscriber_number,
                    notes=notes,
                    is_active=is_active,
                ),
                "current_user": current_user,
                "sidebar_stats": sidebar_stats,
                "people": people,
                "organizations": organizations,
            },
            status_code=400,
        )


@router.delete("/{subscriber_id}", response_class=HTMLResponse)
@router.post("/{subscriber_id}/delete", response_class=HTMLResponse)
def subscriber_delete(
    request: Request,
    subscriber_id: UUID,
    db: Session = Depends(get_db),
):
    """Delete a subscriber (soft delete)."""
    try:
        web_subscriber_actions_service.delete_subscriber(db=db, subscriber_id=subscriber_id)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="delete",
            entity_type="subscriber",
            entity_id=str(subscriber_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        )

        if request.headers.get("HX-Request"):
            return HTMLResponse(
                content="",
                headers={"HX-Redirect": "/admin/subscribers"},
            )
        return RedirectResponse(url="/admin/subscribers", status_code=303)
    except HTTPException as exc:
        if request.headers.get("HX-Request"):
            return _htmx_error_response(str(exc.detail), status_code=200, reswap="none")
        raise
    except IntegrityError:
        db.rollback()
        message = "Cannot delete subscriber. Linked records exist."
        if request.headers.get("HX-Request"):
            return _htmx_error_response(message, status_code=200, reswap="none")
        raise HTTPException(status_code=409, detail=message)
    except Exception as e:
        from app.web.admin import get_current_user, get_sidebar_stats
        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        return templates.TemplateResponse(
            "admin/errors/500.html",
            {
                "request": request,
                "error": str(e),
                "current_user": current_user,
                "sidebar_stats": sidebar_stats,
            },
            status_code=500,
        )


# Bulk action routes
@router.post("/bulk/status", response_class=HTMLResponse)
def bulk_status_change(
    request: Request,
    body: dict = Depends(parse_json_body),
    db: Session = Depends(get_db),
):
    """Bulk activate or deactivate subscribers."""
    try:
        ids = body.get("subscriber_ids", [])
        status = body.get("status", "")

        if not ids:
            return _htmx_error_response("No subscribers selected", title="Error", reswap="none")

        if status not in ("active", "inactive"):
            return _htmx_error_response("Invalid status", title="Error", reswap="none")

        is_active = status == "active"
        updated_count = web_subscriber_actions_service.bulk_set_subscriber_status(
            db=db,
            subscriber_ids=ids,
            is_active=is_active,
        )

        trigger = {
            "showToast": {
                "type": "success",
                "title": "Status updated",
                "message": f"{updated_count} subscriber(s) set to {'active' if is_active else 'inactive'}.",
            }
        }
        return Response(
            status_code=200,
            headers={"HX-Trigger": json.dumps(trigger), "HX-Refresh": "true"},
        )
    except Exception as e:
        return _htmx_error_response(str(e), title="Error", reswap="none")


@router.post("/bulk/delete", response_class=HTMLResponse)
def bulk_delete(
    request: Request,
    body: dict = Depends(parse_json_body),
    db: Session = Depends(get_db),
):
    """Bulk delete inactive subscribers."""
    try:
        ids = body.get("subscriber_ids", [])

        if not ids:
            return _htmx_error_response("No subscribers selected", title="Error", reswap="none")

        deleted_count, skipped_active = web_subscriber_actions_service.bulk_delete_inactive_subscribers(
            db=db,
            subscriber_ids=ids,
        )

        message_parts = [f"{deleted_count} subscriber(s) deleted"]
        if skipped_active > 0:
            message_parts.append(f"{skipped_active} active (skipped)")

        trigger = {
            "showToast": {
                "type": "success" if deleted_count > 0 else "warning",
                "title": "Bulk delete complete",
                "message": ". ".join(message_parts) + ".",
            }
        }
        return Response(
            status_code=200,
            headers={"HX-Trigger": json.dumps(trigger), "HX-Refresh": "true"},
        )
    except Exception as e:
        return _htmx_error_response(str(e), title="Error", reswap="none")
