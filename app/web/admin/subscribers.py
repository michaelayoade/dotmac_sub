"""Admin subscriber management web routes."""

import json

from fastapi import APIRouter, Depends, Form, Query, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from typing import Optional
from uuid import UUID

from app.db import SessionLocal
from app.models.person import Person
from app.models.subscriber import SubscriberAccount
from app.models.tickets import Ticket, TicketStatus
from app.services import auth as auth_service
from app.services import subscriber as subscriber_service
from app.services import audit as audit_service
from app.services.audit_helpers import build_changes_metadata, extract_changes, format_changes, log_audit_event
from app.services.crm import conversation as crm_service
from app.services import notification as notification_service
from app.models.auth import AuthProvider
from app.models.catalog import ContractTerm, OfferStatus, SubscriptionStatus
from app.schemas.auth import UserCredentialCreate
from app.schemas.subscriber import SubscriberAccountCreate, SubscriberUpdate
from app.services.auth_flow import hash_password
from app.services import catalog as catalog_service

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/subscribers", tags=["web-admin-subscribers"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _parse_customer_ref(value: str | None) -> tuple[str, UUID]:
    if not value:
        raise ValueError("customer_ref is required")
    if ":" not in value:
        raise ValueError("customer_ref must be selected from the list")
    ref_type, ref_id = value.split(":", 1)
    if ref_type not in ("person", "organization"):
        raise ValueError("customer_ref must be person or organization")
    return ref_type, UUID(ref_id)


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


def _resolve_customer_ref(
    customer_ref: str | None,
    customer_search: str | None,
    db: Session,
) -> tuple[str, UUID] | None:
    if customer_ref:
        return _parse_customer_ref(customer_ref)
    search_term = (customer_search or "").strip()
    if not search_term:
        return None
    from app.services import customer_search as customer_search_service

    matches = customer_search_service.search(db=db, query=search_term, limit=2)
    if len(matches) == 1:
        return _parse_customer_ref(matches[0].get("ref"))
    if not matches:
        raise ValueError("No customer matches that search")
    raise ValueError("customer_ref must be selected from the list")


def _resolve_person_for_org(db: Session, org_id: UUID) -> UUID | None:
    return (
        db.query(Person.id)
        .filter(Person.organization_id == org_id)
        .order_by(Person.created_at.asc())
        .scalar()
    )


@router.get("", response_class=HTMLResponse)
def subscribers_list(
    request: Request,
    search: Optional[str] = None,
    subscriber_type: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List all subscribers with search and filtering."""
    offset = (page - 1) * per_page

    subscribers = subscriber_service.subscribers.list(
        db=db,
        subscriber_type=subscriber_type if subscriber_type else None,
        person_id=None,
        organization_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )

    # Get total count for pagination
    all_subscribers = subscriber_service.subscribers.list(
        db=db,
        subscriber_type=subscriber_type if subscriber_type else None,
        person_id=None,
        organization_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=10000,
        offset=0,
    )
    total = len(all_subscribers)
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
    from app.web.admin import get_sidebar_stats, get_current_user
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
    from app.web.admin import get_sidebar_stats, get_current_user
    from app.services import person as person_service

    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)
    person_id = request.query_params.get("person_id", "").strip()
    organization_id = request.query_params.get("organization_id", "").strip()
    prefill_ref = ""
    prefill_label = ""
    if person_id:
        try:
            person = person_service.people.get(db=db, person_id=person_id)
            prefill_ref = f"person:{person.id}"
            prefill_label = f"{person.first_name} {person.last_name}"
            if person.email:
                prefill_label = f"{prefill_label} ({person.email})"
        except Exception:
            prefill_ref = ""
            prefill_label = ""
    elif organization_id:
        try:
            organization = subscriber_service.organizations.get(
                db=db,
                organization_id=organization_id,
            )
            prefill_ref = f"organization:{organization.id}"
            prefill_label = organization.name
            if organization.domain:
                prefill_label = f"{prefill_label} ({organization.domain})"
        except Exception:
            prefill_ref = ""
            prefill_label = ""

    # Fetch lookup data for dropdowns
    people = person_service.people.list(
        db=db,
        email=None,
        status=None,
        party_status=None,
        organization_id=None,
        is_active=True,
        order_by="last_name",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    organizations = subscriber_service.organizations.list(
        db=db,
        name=None,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )

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
    customer_ref: Optional[str] = Form(None),
    customer_search: Optional[str] = Form(None),
    subscriber_type: Optional[str] = Form(None),
    person_id: Optional[str] = Form(None),
    organization_id: Optional[str] = Form(None),
    subscriber_number: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    is_active: Optional[str] = Form(None),
    create_user: Optional[str] = Form(None),
    user_username: Optional[str] = Form(None),
    user_password: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Create a new subscriber."""
    try:
        from app.schemas.subscriber import SubscriberCreate

        resolved_ref = _resolve_customer_ref(customer_ref, customer_search, db)
        if resolved_ref:
            subscriber_type, ref_id = resolved_ref
            person_uuid = ref_id if subscriber_type == "person" else None
            org_uuid = ref_id if subscriber_type == "organization" else None
        else:
            if subscriber_type not in ("person", "organization"):
                raise ValueError("customer_ref is required")
            person_uuid = UUID(person_id) if person_id else None
            org_uuid = UUID(organization_id) if organization_id else None
            if subscriber_type == "person" and not person_uuid:
                raise ValueError("person_id is required for person subscribers")
            if subscriber_type == "organization" and not org_uuid:
                raise ValueError("organization_id is required for organization subscribers")
        if subscriber_type == "organization" and org_uuid and not person_uuid:
            person_uuid = _resolve_person_for_org(db, org_uuid)
        if not person_uuid:
            raise ValueError("person_id is required")
        if create_user == "true":
            if subscriber_type != "person" or not person_uuid:
                raise ValueError("Customer portal logins can only be created for person subscribers.")
            if not user_username or not user_password:
                raise ValueError("Username and password are required to create a login.")
            existing = auth_service.user_credentials.list(
                db=db,
                person_id=str(person_uuid),
                provider=AuthProvider.local.value,
                is_active=True,
                order_by="created_at",
                order_dir="desc",
                limit=1,
                offset=0,
            )
            if existing:
                raise ValueError("Customer already has a portal login.")
        data = SubscriberCreate(
            person_id=person_uuid,
            subscriber_number=subscriber_number.strip() if subscriber_number else None,
            notes=notes.strip() if notes else None,
            is_active=is_active == "true",
        )
        subscriber = subscriber_service.subscribers.create(db=db, payload=data)
        subscriber_service.accounts.create(
            db=db,
            payload=SubscriberAccountCreate(subscriber_id=subscriber.id),
        )
        if create_user == "true":
            credential = UserCredentialCreate(
                person_id=person_uuid,
                provider=AuthProvider.local,
                username=user_username.strip(),
                password_hash=hash_password(user_password),
            )
            auth_service.user_credentials.create(db=db, payload=credential)
        return RedirectResponse(
            url=f"/admin/subscribers/{subscriber.id}",
            status_code=303,
        )
    except HTTPException:
        raise
    except Exception as e:
        from app.web.admin import get_sidebar_stats, get_current_user
        from app.services import person as person_service

        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)

        # Fetch lookup data for dropdowns on error
        people = person_service.people.list(
            db=db,
            email=None,
            status=None,
            party_status=None,
            organization_id=None,
            is_active=True,
            order_by="last_name",
            order_dir="asc",
            limit=500,
            offset=0,
        )
        organizations = subscriber_service.organizations.list(
            db=db,
            name=None,
            order_by="name",
            order_dir="asc",
            limit=500,
            offset=0,
        )

        return templates.TemplateResponse(
            "admin/subscribers/form.html",
            {
                "request": request,
                "subscriber": None,
                "action": "create",
                "error": str(e),
                "form": {
                    "customer_ref": customer_ref or "",
                    "customer_search": customer_search or "",
                    "subscriber_type": subscriber_type or "",
                    "person_id": person_id or "",
                    "organization_id": organization_id or "",
                    "subscriber_number": subscriber_number or "",
                    "notes": notes or "",
                    "is_active": is_active == "true",
                    "create_user": create_user == "true",
                    "user_username": user_username or "",
                },
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
    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
    if not subscriber:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Subscriber not found"},
            status_code=404,
        )

    # Get subscriber's subscriptions (via their accounts)
    from app.services import billing as billing_service
    from app.services import tickets as tickets_service

    subscriptions = []
    online_status = {}  # subscription_id -> is_online
    try:
        # Get all account IDs for this subscriber
        account_ids = [str(acc.id) for acc in (subscriber.accounts or [])]
        if account_ids:
            # Fetch subscriptions for all subscriber's accounts
            for account_id in account_ids:
                acct_subs = catalog_service.subscriptions.list(
                    db=db,
                    account_id=account_id,
                    offer_id=None,
                    status=None,
                    order_by="created_at",
                    order_dir="desc",
                    limit=10,
                    offset=0,
                )
                subscriptions.extend(acct_subs)
            # Sort by created_at desc and limit
            subscriptions = sorted(subscriptions, key=lambda s: s.created_at or s.id, reverse=True)[:10]

            # Check online status from RADIUS sessions
            from app.models.usage import RadiusAccountingSession, AccountingStatus
            from sqlalchemy import and_, desc

            for sub in subscriptions:
                # Find most recent RADIUS session for this subscription
                latest_session = (
                    db.query(RadiusAccountingSession)
                    .filter(RadiusAccountingSession.subscription_id == sub.id)
                    .order_by(desc(RadiusAccountingSession.created_at))
                    .first()
                )
                if latest_session:
                    # Online if last record is start/interim (not stop) and no session_end
                    is_online = (
                        latest_session.status_type in (AccountingStatus.start, AccountingStatus.interim)
                        and latest_session.session_end is None
                    )
                    online_status[str(sub.id)] = is_online
                else:
                    online_status[str(sub.id)] = False  # No session = offline
    except Exception:
        pass

    # Get subscriber's billing accounts and invoices
    invoices = []
    balance_due = 0
    accounts = []
    try:
        accounts = subscriber_service.accounts.list(
            db=db,
            subscriber_id=str(subscriber_id),
            reseller_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=25,
            offset=0,
        )
        if accounts:
            account = accounts[0]
            invoices = billing_service.invoices.list(
                db=db,
                account_id=account.id,
                status=None,
                is_active=None,
                order_by="created_at",
                order_dir="desc",
                limit=5,
                offset=0,
            )
            # Calculate balance due
            def get_status(obj):
                status = getattr(obj, "status", "")
                return status.value if hasattr(status, "value") else str(status)

            balance_due = sum(
                float(getattr(inv, "total_amount", 0) or 0)
                for inv in invoices
                if get_status(inv) in ("pending", "sent", "overdue")
            )
    except Exception:
        pass

    # Get subscriber's tickets scoped to their accounts
    tickets = []
    open_tickets_count = 0
    try:
        account_ids = [account.id for account in subscriber.accounts or []]
        if account_ids:
            tickets = (
                db.query(Ticket)
                .filter(Ticket.account_id.in_(account_ids))
                .filter(Ticket.is_active.is_(True))
                .order_by(Ticket.created_at.desc())
                .limit(5)
                .all()
            )
            open_tickets_count = (
                db.query(Ticket)
                .filter(Ticket.account_id.in_(account_ids))
                .filter(Ticket.is_active.is_(True))
                .filter(Ticket.status.in_([TicketStatus.new, TicketStatus.open, TicketStatus.in_progress]))
                .count()
            )
    except Exception:
        pass

    # Get subscriber's conversations (from CRM inbox)
    conversations = []
    try:
        if subscriber.person_id:
            conversations = crm_service.Conversations.list(
                db=db,
                person_id=str(subscriber.person_id),
                ticket_id=None,
                status=None,
                is_active=True,
                order_by="last_message_at",
                order_dir="desc",
                limit=5,
                offset=0,
            )
    except Exception:
        pass

    # Get notifications sent to this subscriber
    notifications = []
    try:
        if subscriber.person:
            person = subscriber.person
            recipients = []
            if person.email:
                recipients.append(person.email)
            if person.phone:
                recipients.append(person.phone)
            if recipients:
                all_notifications = notification_service.Notifications.list(
                    db=db,
                    channel=None,
                    status=None,
                    is_active=True,
                    order_by="created_at",
                    order_dir="desc",
                    limit=100,
                    offset=0,
                )
                # Filter by recipient (since list doesn't support recipients param yet)
                notifications = [n for n in all_notifications if n.recipient in recipients][:5]
    except Exception:
        pass

    # Calculate stats
    monthly_bill = sum(
        float(getattr(sub, "price", 0) or 0) for sub in subscriptions
    ) if subscriptions else 0

    stats = {
        "monthly_bill": monthly_bill,
        "balance_due": balance_due,
        "data_usage": "0",  # Would come from RADIUS/monitoring
        "open_tickets": open_tickets_count,
    }

    # Get subscriber's addresses with coordinates for mini-map
    import math
    from app.models.subscriber import Address
    from app.models.network import FdhCabinet, FiberSpliceClosure

    addresses = db.query(Address).filter(Address.subscriber_id == subscriber_id).all()
    primary_address = next((a for a in addresses if a.is_primary), addresses[0] if addresses else None)

    # Build mini-map data if address has coordinates
    map_data = None
    if primary_address and primary_address.latitude and primary_address.longitude:
        features = []
        # Add customer location
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [primary_address.longitude, primary_address.latitude]},
            "properties": {
                "type": "customer",
                "name": f"{subscriber.person.first_name} {subscriber.person.last_name}" if subscriber.person else (subscriber.organization.name if subscriber.organization else "Customer"),
                "address": primary_address.address_line1,
            },
        })

        # Find nearby FDH cabinets (within ~2km radius)
        def haversine_distance(lat1, lon1, lat2, lon2):
            R = 6371000  # Earth's radius in meters
            phi1, phi2 = math.radians(lat1), math.radians(lat2)
            delta_phi = math.radians(lat2 - lat1)
            delta_lambda = math.radians(lon2 - lon1)
            a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
            return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

        fdh_cabinets = db.query(FdhCabinet).filter(
            FdhCabinet.is_active.is_(True),
            FdhCabinet.latitude.isnot(None),
            FdhCabinet.longitude.isnot(None)
        ).all()

        for fdh in fdh_cabinets:
            distance = haversine_distance(primary_address.latitude, primary_address.longitude, fdh.latitude, fdh.longitude)
            if distance <= 2000:  # Within 2km
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [fdh.longitude, fdh.latitude]},
                    "properties": {
                        "type": "fdh_cabinet",
                        "name": fdh.name,
                        "code": fdh.code,
                        "distance_m": round(distance),
                    },
                })

        # Find nearby splice closures
        closures = db.query(FiberSpliceClosure).filter(
            FiberSpliceClosure.is_active.is_(True),
            FiberSpliceClosure.latitude.isnot(None),
            FiberSpliceClosure.longitude.isnot(None)
        ).all()

        for closure in closures:
            distance = haversine_distance(primary_address.latitude, primary_address.longitude, closure.latitude, closure.longitude)
            if distance <= 1000:  # Within 1km
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [closure.longitude, closure.latitude]},
                    "properties": {
                        "type": "splice_closure",
                        "name": closure.name,
                        "distance_m": round(distance),
                    },
                })

        map_data = {
            "center": [primary_address.latitude, primary_address.longitude],
            "geojson": {"type": "FeatureCollection", "features": features},
        }

    audit_events = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type="subscriber",
        entity_id=str(subscriber_id),
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    actor_ids = {str(event.actor_id) for event in audit_events if getattr(event, "actor_id", None)}
    people = {}
    if actor_ids:
        people = {
            str(person.id): person
            for person in db.query(Person).filter(Person.id.in_(actor_ids)).all()
        }
    timeline = []
    for event in audit_events:
        actor = people.get(str(event.actor_id)) if getattr(event, "actor_id", None) else None
        actor_name = f"{actor.first_name} {actor.last_name}".strip() if actor else "System"
        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes, max_items=2)
        detail = actor_name
        if change_summary:
            detail = f"{detail} · {change_summary}"
        timeline.append(
            {
                "type": "audit",
                "title": (event.action or "Activity").replace("_", " ").title(),
                "detail": detail,
                "time": event.occurred_at.strftime("%b %d, %Y %H:%M")
                if event.occurred_at
                else "Just now",
            }
        )

    # Get sidebar stats and current user
    from app.web.admin import get_sidebar_stats, get_current_user
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    # Get active offers for subscription modal
    offers = catalog_service.offers.list(
        db=db,
        service_type=None,
        access_type=None,
        status=OfferStatus.active.value,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )

    return templates.TemplateResponse(
        "admin/subscribers/detail.html",
        {
            "request": request,
            "subscriber": subscriber,
            "accounts": accounts,
            "subscriptions": subscriptions,
            "online_status": online_status,
            "invoices": invoices,
            "tickets": tickets,
            "conversations": conversations,
            "notifications": notifications,
            "stats": stats,
            "addresses": addresses,
            "primary_address": primary_address,
            "map_data": map_data,
            "timeline": timeline,
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
            "offers": offers,
            "subscription_statuses": [s.value for s in SubscriptionStatus],
            "contract_terms": [t.value for t in ContractTerm],
        },
    )


@router.post("/{subscriber_id}/deactivate", response_class=HTMLResponse)
def subscriber_deactivate(
    request: Request,
    subscriber_id: UUID,
    db: Session = Depends(get_db),
):
    """Deactivate a subscriber before deletion."""
    before = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
    subscriber_service.subscribers.update(
        db=db,
        subscriber_id=str(subscriber_id),
        payload=SubscriberUpdate(is_active=False),
    )
    after = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
    metadata = build_changes_metadata(before, after)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="subscriber",
        entity_id=str(subscriber_id),
        actor_id=str(current_user.get("person_id")) if current_user else None,
        metadata=metadata,
    )
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    return RedirectResponse(url=f"/admin/subscribers/{subscriber_id}", status_code=303)


@router.get("/{subscriber_id}/suspend", response_class=HTMLResponse)
def subscriber_suspend(request: Request, subscriber_id: str, db: Session = Depends(get_db)):
    from app.web.admin import get_sidebar_stats, get_current_user
    from app.models.subscriber import Account

    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
    if not subscriber:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Subscriber not found"},
            status_code=404,
        )

    accounts = db.query(Account).filter(Account.subscriber_id == subscriber.id).all()

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
    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
    if not subscriber:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Subscriber not found"},
            status_code=404,
        )

    from app.web.admin import get_sidebar_stats, get_current_user
    from app.services import person as person_service

    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    # Fetch lookup data for dropdowns
    people = person_service.people.list(
        db=db,
        email=None,
        status=None,
        party_status=None,
        organization_id=None,
        is_active=True,
        order_by="last_name",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    organizations = subscriber_service.organizations.list(
        db=db,
        name=None,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )

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
    customer_ref: Optional[str] = Form(None),
    customer_search: Optional[str] = Form(None),
    subscriber_type: Optional[str] = Form(None),
    person_id: Optional[str] = Form(None),
    organization_id: Optional[str] = Form(None),
    subscriber_number: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    is_active: Optional[str] = Form(None),  # Checkbox sends "true" or nothing
    db: Session = Depends(get_db),
):
    """Update a subscriber."""
    try:
        from app.schemas.subscriber import SubscriberUpdate

        before = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
        resolved_ref = _resolve_customer_ref(customer_ref, customer_search, db)
        if resolved_ref:
            subscriber_type, ref_id = resolved_ref
            person_uuid = ref_id if subscriber_type == "person" else None
            org_uuid = ref_id if subscriber_type == "organization" else None
        else:
            if subscriber_type not in ("person", "organization"):
                raise ValueError("customer_ref is required")
            person_uuid = UUID(person_id) if person_id else None
            org_uuid = UUID(organization_id) if organization_id else None
            if subscriber_type == "person" and not person_uuid:
                raise ValueError("person_id is required for person subscribers")
            if subscriber_type == "organization" and not org_uuid:
                raise ValueError("organization_id is required for organization subscribers")
        active = is_active == "true"
        data = SubscriberUpdate(
            person_id=person_uuid,
            subscriber_number=subscriber_number.strip() if subscriber_number else None,
            notes=notes.strip() if notes else None,
            is_active=active,
        )
        subscriber = subscriber_service.subscribers.update(
            db=db, subscriber_id=subscriber_id, payload=data
        )
        after = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
        metadata = build_changes_metadata(before, after)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        try:
            log_audit_event(
                db=db,
                request=request,
                action="update",
                entity_type="subscriber",
                entity_id=str(subscriber_id),
                actor_id=str(current_user.get("person_id")) if current_user else None,
                metadata=metadata,
            )
        except Exception:
            db.rollback()
        return RedirectResponse(
            url=f"/admin/subscribers/{subscriber_id}",
            status_code=303,
        )
    except Exception as e:
        from app.web.admin import get_sidebar_stats, get_current_user
        from app.services import person as person_service

        db.rollback()
        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)

        # Fetch lookup data for dropdowns on error
        people = person_service.people.list(
            db=db,
            email=None,
            status=None,
            is_active=True,
            order_by="last_name",
            order_dir="asc",
            limit=500,
            offset=0,
        )
        organizations = subscriber_service.organizations.list(
            db=db,
            name=None,
            order_by="name",
            order_dir="asc",
            limit=500,
            offset=0,
        )

        return templates.TemplateResponse(
            "admin/subscribers/form.html",
            {
                "request": request,
                "subscriber": subscriber,
                "action": "edit",
                "error": str(e),
                "form": {
                    "customer_ref": customer_ref or "",
                    "customer_search": customer_search or "",
                    "subscriber_type": subscriber_type or "",
                    "person_id": person_id or "",
                    "organization_id": organization_id or "",
                    "subscriber_number": subscriber_number or "",
                    "notes": notes or "",
                    "is_active": is_active == "true",
                },
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
        subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
        if subscriber.is_active:
            raise HTTPException(status_code=409, detail="Deactivate subscriber before deleting.")
        if db.query(SubscriberAccount.id).filter(SubscriberAccount.subscriber_id == subscriber.id).first():
            raise HTTPException(status_code=409, detail="Delete subscriber accounts before deleting subscriber.")
        subscriber_service.subscribers.delete(db=db, subscriber_id=str(subscriber_id))
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="delete",
            entity_type="subscriber",
            entity_id=str(subscriber_id),
            actor_id=str(current_user.get("person_id")) if current_user else None,
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
        from app.web.admin import get_sidebar_stats, get_current_user
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
async def bulk_status_change(
    request: Request,
    db: Session = Depends(get_db),
):
    """Bulk activate or deactivate subscribers."""
    try:
        body = await request.json()
        ids = body.get("subscriber_ids", [])
        status = body.get("status", "")

        if not ids:
            return _htmx_error_response("No subscribers selected", title="Error", reswap="none")

        if status not in ("active", "inactive"):
            return _htmx_error_response("Invalid status", title="Error", reswap="none")

        is_active = status == "active"
        updated_count = 0

        for subscriber_id in ids:
            try:
                subscriber_service.subscribers.update(
                    db=db,
                    subscriber_id=str(subscriber_id),
                    payload=SubscriberUpdate(is_active=is_active),
                )
                updated_count += 1
            except Exception:
                continue

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
async def bulk_delete(
    request: Request,
    db: Session = Depends(get_db),
):
    """Bulk delete inactive subscribers."""
    try:
        body = await request.json()
        ids = body.get("subscriber_ids", [])

        if not ids:
            return _htmx_error_response("No subscribers selected", title="Error", reswap="none")

        deleted_count = 0
        skipped_active = 0
        skipped_has_accounts = 0

        for subscriber_id in ids:
            try:
                subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
                if subscriber.is_active:
                    skipped_active += 1
                    continue
                if db.query(SubscriberAccount.id).filter(SubscriberAccount.subscriber_id == subscriber.id).first():
                    skipped_has_accounts += 1
                    continue
                subscriber_service.subscribers.delete(db=db, subscriber_id=str(subscriber_id))
                deleted_count += 1
            except Exception:
                continue

        message_parts = [f"{deleted_count} subscriber(s) deleted"]
        if skipped_active > 0:
            message_parts.append(f"{skipped_active} active (skipped)")
        if skipped_has_accounts > 0:
            message_parts.append(f"{skipped_has_accounts} have accounts (skipped)")

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
