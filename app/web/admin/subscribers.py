"""Admin subscriber management web routes."""

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

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
from app.models.notification import NotificationChannel, NotificationStatus
from app.models.subscriber import Subscriber
from app.schemas.notification import NotificationCreate
from app.schemas.subscriber import SubscriberUpdate
from app.services import audit as audit_service
from app.services import notification as notification_service
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


def _actor_id(request: Request) -> str | None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    return str(current_user.get("subscriber_id")) if current_user else None


def _parse_mentioned_subscriber_ids(raw_ids: str | None) -> list[UUID]:
    if not raw_ids:
        return []
    text = raw_ids.strip()
    if not text:
        return []

    candidates: list[object]
    try:
        parsed = json.loads(text)
        candidates = parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        candidates = [part.strip() for part in text.split(",") if part.strip()]

    mentioned_ids: list[UUID] = []
    seen: set[str] = set()
    for value in candidates:
        try:
            subscriber_id = UUID(str(value))
        except (TypeError, ValueError):
            continue
        key = str(subscriber_id)
        if key in seen:
            continue
        seen.add(key)
        mentioned_ids.append(subscriber_id)
    return mentioned_ids


def _notify_tagged_subscribers(
    db: Session,
    request: Request,
    *,
    subscriber_id: UUID,
    comment: str,
    mentioned_subscriber_ids: list[UUID],
) -> tuple[int, list[dict[str, str]]]:
    if not mentioned_subscriber_ids:
        return 0, []

    actor_subscriber_id = _actor_id(request)
    recipients = (
        db.query(Subscriber)
        .filter(Subscriber.id.in_(mentioned_subscriber_ids))
        .filter(Subscriber.is_active.is_(True))
        .all()
    )

    notified = 0
    resolved_mentions: list[dict[str, str]] = []
    base_url = str(request.base_url).rstrip("/")
    subscriber_url = f"{base_url}/admin/subscribers/{subscriber_id}"
    short_subscriber_id = str(subscriber_id)[:8]
    subject = f"You were mentioned on Subscriber {short_subscriber_id}"
    body = (
        f"You were tagged in a subscriber comment.\n\n"
        f"Comment:\n{comment}\n\n"
        f"Open subscriber: {subscriber_url}"
    )

    for tagged_subscriber in recipients:
        display_name = (
            tagged_subscriber.display_name
            or f"{tagged_subscriber.first_name or ''} {tagged_subscriber.last_name or ''}".strip()
            or tagged_subscriber.email
            or str(tagged_subscriber.id)
        )
        resolved_mentions.append(
            {"id": str(tagged_subscriber.id), "name": display_name}
        )
        if actor_subscriber_id and str(tagged_subscriber.id) == actor_subscriber_id:
            continue

        notification_service.notifications.create(
            db,
            NotificationCreate(
                channel=NotificationChannel.push,
                recipient=str(tagged_subscriber.id),
                subject=subject,
                body=body,
                status=NotificationStatus.delivered,
                sent_at=datetime.now(UTC),
            ),
        )
        if tagged_subscriber.email:
            notification_service.notifications.create(
                db,
                NotificationCreate(
                    channel=NotificationChannel.email,
                    recipient=tagged_subscriber.email,
                    subject=subject,
                    body=body,
                    status=NotificationStatus.queued,
                ),
            )
        notified += 1
    return notified, resolved_mentions


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
    subscriber_category: str | None = Form(None),
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
            subscriber_category=subscriber_category,
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
        # Reset failed transaction state before loading sidebar/options for error view.
        db.rollback()
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
                    subscriber_category=subscriber_category,
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


@router.post("/{subscriber_id}/comments", response_class=HTMLResponse)
def subscriber_add_comment(
    request: Request,
    subscriber_id: UUID,
    comment: str = Form(...),
    is_todo: str | None = Form(None),
    mention_ids: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Add a comment to the subscriber activity timeline."""
    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=str(subscriber_id))
    if not subscriber:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Subscriber not found"},
            status_code=404,
        )

    cleaned_comment = comment.strip()
    if not cleaned_comment:
        return RedirectResponse(url=f"/admin/subscribers/{subscriber_id}", status_code=303)
    mentioned_subscriber_ids = _parse_mentioned_subscriber_ids(mention_ids)
    notified_count, resolved_mentions = _notify_tagged_subscribers(
        db,
        request,
        subscriber_id=subscriber_id,
        comment=cleaned_comment,
        mentioned_subscriber_ids=mentioned_subscriber_ids,
    )
    log_audit_event(
        db=db,
        request=request,
        action="comment",
        entity_type="subscriber",
        entity_id=str(subscriber_id),
        actor_id=_actor_id(request),
        metadata={
            "comment": cleaned_comment,
            "is_todo": bool(is_todo),
            "is_completed": False,
            "mentions": resolved_mentions,
            "notified_users": notified_count,
        },
    )
    return RedirectResponse(url=f"/admin/subscribers/{subscriber_id}", status_code=303)


@router.post("/{subscriber_id}/comments/{event_id}/toggle", response_class=HTMLResponse)
def subscriber_toggle_comment_todo(
    request: Request,
    subscriber_id: UUID,
    event_id: UUID,
    db: Session = Depends(get_db),
):
    """Toggle todo completion state for a subscriber comment event."""
    event = audit_service.audit_events.get(db=db, event_id=str(event_id))
    if (
        event.entity_type != "subscriber"
        or str(event.entity_id) != str(subscriber_id)
        or event.action != "comment"
    ):
        raise HTTPException(status_code=404, detail="Comment not found")

    metadata = dict(getattr(event, "metadata_", None) or {})
    if not metadata.get("is_todo"):
        return RedirectResponse(url=f"/admin/subscribers/{subscriber_id}", status_code=303)

    current_completed = bool(metadata.get("is_completed"))
    metadata["is_completed"] = not current_completed
    event.metadata_ = metadata
    db.add(event)
    db.commit()

    log_audit_event(
        db=db,
        request=request,
        action="comment_todo_toggle",
        entity_type="subscriber",
        entity_id=str(subscriber_id),
        actor_id=_actor_id(request),
        metadata={
            "source_comment_event_id": str(event_id),
            "is_completed": metadata["is_completed"],
        },
    )
    return RedirectResponse(url=f"/admin/subscribers/{subscriber_id}", status_code=303)


@router.post(
    "/addresses/{address_id}/geocode",
    response_class=JSONResponse,
)
def geocode_address(
    address_id: str,
    latitude: float = Body(...),
    longitude: float = Body(...),
    db: Session = Depends(get_db),
):
    """Update subscriber address coordinates from geocoding/manual selection."""
    from app.schemas.subscriber import AddressUpdate

    try:
        parsed_address_id = UUID(address_id)
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
    "/{subscriber_id}/geocode-primary",
    response_class=JSONResponse,
)
def geocode_primary_address(
    subscriber_id: UUID,
    latitude: float = Body(...),
    longitude: float = Body(...),
    db: Session = Depends(get_db),
):
    """Save coordinates to a primary address, creating one from profile address if missing."""
    from app.schemas.subscriber import AddressCreate, AddressUpdate

    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=str(subscriber_id))
    if not subscriber:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    addresses = subscriber_service.addresses.list(
        db=db,
        subscriber_id=str(subscriber_id),
        order_by="created_at",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    primary_address = next((addr for addr in addresses if addr.is_primary), addresses[0] if addresses else None)

    created = False
    if primary_address is None:
        if not (subscriber.address_line1 or "").strip():
            raise HTTPException(
                status_code=400,
                detail="No address exists to geolocate. Add an address first.",
            )
        primary_address = subscriber_service.addresses.create(
            db=db,
            payload=AddressCreate(
                subscriber_id=subscriber_id,
                address_line1=subscriber.address_line1,
                address_line2=subscriber.address_line2,
                city=subscriber.city,
                region=subscriber.region,
                postal_code=subscriber.postal_code,
                country_code=subscriber.country_code,
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
    subscriber_category: str | None = Form(None),
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
            subscriber_category=subscriber_category,
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
                    subscriber_category=subscriber_category,
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


@router.post("/{subscriber_id}/billing-config", response_class=HTMLResponse)
def subscriber_billing_config_update(
    subscriber_id: UUID,
    billing_day: str | None = Form(None),
    payment_due_days: str | None = Form(None),
    grace_period_days: str | None = Form(None),
    min_balance: str | None = Form(None),
    billing_enabled: str | None = Form(None),
    blocking_period_days: str | None = Form(None),
    deactivation_period_days: str | None = Form(None),
    auto_create_invoices: str | None = Form(None),
    send_billing_notifications: str | None = Form(None),
    db: Session = Depends(get_db),
):
    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=str(subscriber_id))
    if not subscriber:
        return RedirectResponse(url="/admin/subscribers", status_code=303)

    def _to_int(value: str | None) -> int | None:
        if value is None or value.strip() == "":
            return None
        return int(value)

    def _to_decimal(value: str | None) -> Decimal | None:
        if value is None or value.strip() == "":
            return None
        try:
            return Decimal(value.strip())
        except InvalidOperation as exc:
            raise HTTPException(status_code=400, detail="Invalid minimum balance") from exc

    payload = SubscriberUpdate(
        billing_day=_to_int(billing_day),
        payment_due_days=_to_int(payment_due_days),
        grace_period_days=_to_int(grace_period_days),
        min_balance=_to_decimal(min_balance),
        billing_enabled=(billing_enabled == "true"),
    )
    subscriber_service.subscribers.update(
        db=db,
        subscriber_id=str(subscriber_id),
        payload=payload,
    )

    metadata = dict(subscriber.metadata_ or {})
    metadata["blocking_period_days"] = _to_int(blocking_period_days) or 0
    metadata["deactivation_period_days"] = _to_int(deactivation_period_days) or 0
    metadata["auto_create_invoices"] = auto_create_invoices == "true"
    metadata["send_billing_notifications"] = send_billing_notifications == "true"
    subscriber_service.subscribers.update(
        db=db,
        subscriber_id=str(subscriber_id),
        payload=SubscriberUpdate(metadata_=metadata),
    )
    return RedirectResponse(url=f"/admin/subscribers/{subscriber_id}", status_code=303)


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
