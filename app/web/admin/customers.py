"""Admin customer (person & organization) management web routes."""

import logging
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, Form, Query, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from typing import Optional

from app.db import SessionLocal
from app.models.auth import ApiKey, MFAMethod, Session, UserCredential
from app.models.billing import Invoice, InvoiceStatus, Payment, PaymentStatus
# TODO: Person model replaced by Subscriber - PartyStatus may need to be defined in subscriber model
# from app.models.person import PartyStatus, Person
from app.models.subscriber import Subscriber
# TODO: person schemas no longer exist - need to use subscriber schemas
# from app.schemas.person import ChannelTypeEnum, PersonChannelCreate, PersonCreate
from app.schemas.subscriber import AccountRoleCreate
from app.models.subscriber import AccountRoleType
from app.models.subscriber import Organization, Subscriber
from app.services.auth_dependencies import require_permission
from app.services import subscriber as subscriber_service
from app.services import audit as audit_service
from app.services.audit_helpers import build_changes_metadata, extract_changes, format_changes, log_audit_event
from app.services import notification as notification_service

logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/customers", tags=["web-admin-customers"])
contacts_router = APIRouter(prefix="/contacts", tags=["web-admin-contacts"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _dedupe_accounts(accounts):
    unique = {}
    for account in accounts:
        unique[str(account.id)] = account
    return list(unique.values())


def _list_subscriptions_for_accounts(db: Session, accounts):
    if not accounts:
        return []
    from app.services import catalog as catalog_service

    subscriptions = []
    for account in accounts:
        try:
            account_subs = catalog_service.subscriptions.list(
                db=db,
                account_id=str(account.id),
                offer_id=None,
                status=None,
                order_by="created_at",
                order_dir="desc",
                limit=200,
                offset=0,
            )
            subscriptions.extend(account_subs)
        except Exception:
            continue
    return subscriptions


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


def _parse_date(value: str | None) -> datetime | None:
    """Parse a date string (YYYY-MM-DD) into a timezone-aware datetime."""
    if not value or not value.strip():
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d")
        return parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# TODO: _ensure_subscriber_for_contact_row - person schemas/services removed, use subscriber_service
def _ensure_subscriber_for_contact_row(db: Session, row: dict, organization_id: str | None = None) -> Subscriber:
    email = row.get("email") or f"contact-{uuid.uuid4().hex}@placeholder.local"
    subscriber = db.query(Subscriber).filter(func.lower(Subscriber.email) == email.lower()).first()
    if subscriber:
        return subscriber
    # TODO: PersonCreate schema no longer exists - create subscriber directly
    # payload = PersonCreate(...)
    # return people_service.create(db=db, payload=payload)
    raise NotImplementedError("PersonCreate schema removed - use subscriber_service.subscribers.create instead")


# TODO: _ensure_subscriber_channel - person services removed
def _ensure_subscriber_channel(
    db: Session,
    subscriber_id: str,
    channel_type: str,  # was ChannelTypeEnum
    address: str,
    is_primary: bool,
) -> None:
    if not address:
        return
    # TODO: people_service.add_channel no longer exists
    # people_service.add_channel(
    #     db=db,
    #     subscriber_id=subscriber_id,
    #     payload=PersonChannelCreate(...),
    # )
    raise NotImplementedError("Person channel service removed")


def _create_account_roles_from_rows(
    db: Session,
    account_id: str,
    contact_rows: list[dict],
    organization_id: str | None = None,
) -> None:
    role_map = {
        "primary": AccountRoleType.primary,
        "billing": AccountRoleType.billing,
        "technical": AccountRoleType.technical,
        "support": AccountRoleType.support,
    }
    for row in contact_rows:
        subscriber = _ensure_subscriber_for_contact_row(db, row, organization_id=organization_id)
        subscriber_service.account_roles.create(
            db=db,
            payload=AccountRoleCreate(
                account_id=account_id,
                subscriber_id=subscriber.id,  # Changed from person_id
                role=role_map.get(row.get("role"), AccountRoleType.primary),
                is_primary=row.get("is_primary", False),
                title=row.get("title") or None,
            ),
        )
        # TODO: Channel functions commented out - person schemas/services removed
        # if row.get("email"):
        #     _ensure_subscriber_channel(
        #         db=db,
        #         subscriber_id=str(subscriber.id),
        #         channel_type="email",
        #         address=row["email"],
        #         is_primary=True,
        #     )
        # if row.get("phone"):
        #     _ensure_subscriber_channel(
        #         db=db,
        #         subscriber_id=str(subscriber.id),
        #         channel_type="phone",
        #         address=row["phone"],
        #         is_primary=True,
        #     )


def _format_account_role(role):
    person = role.person if role else None
    return {
        "id": getattr(role, "id", None),
        "first_name": person.first_name if person else "",
        "last_name": person.last_name if person else "",
        "role": role.role if role else None,
        "title": role.title if role else None,
        "is_primary": role.is_primary if role else False,
        "email": person.email if person else "",
        "phone": person.phone if person else "",
    }


def _contacts_base_context(request: Request, db: Session, active_page: str = "contacts"):
    """Base context for contacts pages."""
    from app.web.admin import get_sidebar_stats, get_current_user

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
    search: Optional[str] = None,
    status: Optional[str] = None,  # 'lead', 'contact', 'customer', or None for all
    entity_type: Optional[str] = None,  # 'person' or 'organization'
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """Unified contacts view - all people and organizations with status filtering."""
    offset = (page - 1) * per_page
    # Guard invalid filter combinations to avoid empty result sets (orgs don't use status).
    if entity_type == "organization" and status:
        status = None

    # Build query for people
    people_query = db.query(Subscriber)

    # Filter by party_status if specified
    if status:
        try:
            party_status = PartyStatus(status)
            people_query = people_query.filter(Subscriber.status == party_status)
        except ValueError:
            pass  # Invalid status, ignore filter

    # Search filter
    if search:
        search_filter = f"%{search}%"
        people_query = people_query.filter(
            (Subscriber.first_name.ilike(search_filter)) |
            (Subscriber.last_name.ilike(search_filter)) |
            (Subscriber.email.ilike(search_filter)) |
            (Subscriber.phone.ilike(search_filter))
        )

    # Get counts for dashboard stats (before pagination)
    leads_count = db.query(func.count(Subscriber.id)).filter(Subscriber.status == PartyStatus.lead).scalar() or 0
    contacts_count = db.query(func.count(Subscriber.id)).filter(Subscriber.status == PartyStatus.contact).scalar() or 0
    customers_count = db.query(func.count(Subscriber.id)).filter(Subscriber.status == PartyStatus.customer).scalar() or 0
    subscribers_count = db.query(func.count(Subscriber.id)).filter(Subscriber.status == PartyStatus.subscriber).scalar() or 0
    orgs_count = db.query(func.count(Organization.id)).scalar() or 0

    contacts = []

    # Use a limited window to globally sort merged results before slicing.
    list_limit = offset + per_page

    # Get people (unless filtering to organizations only)
    if entity_type != "organization":
        people = (
            people_query
            .order_by(Subscriber.created_at.desc())
            .limit(list_limit)
            .offset(0)
            .all()
        )
        for p in people:
            contacts.append({
                "id": str(p.id),
                "type": "person",
                "name": f"{p.first_name} {p.last_name}".strip(),
                "email": p.email,
                "phone": p.phone,
                "status": p.status.value if p.status else "active",
                "organization": p.organization.name if p.organization else None,
                "is_active": p.is_active,
                "created_at": p.created_at,
                "raw": p,
            })

    # Get organizations (unless filtering to people only, or filtering by party_status which doesn't apply to orgs)
    if entity_type != "person" and not status:
        orgs_query = db.query(Organization)
        if search:
            orgs_query = orgs_query.filter(Organization.name.ilike(f"%{search}%"))
        orgs = (
            orgs_query
            .order_by(Organization.created_at.desc())
            .limit(list_limit)
            .offset(0)
            .all()
        )
        for o in orgs:
            contacts.append({
                "id": str(o.id),
                "type": "organization",
                "name": o.name,
                "email": getattr(o, "email", None),
                "phone": getattr(o, "phone", None),
                "status": "organization",
                "organization": None,
                "is_active": getattr(o, "is_active", True),
                "created_at": o.created_at,
                "raw": o,
            })

    # Sort combined list by created_at desc
    contacts.sort(key=lambda x: x["created_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    # Calculate totals for pagination
    people_total = people_query.count() if entity_type != "organization" else 0
    org_total = 0
    if entity_type != "person" and not status:
        org_query = db.query(func.count(Organization.id))
        if search:
            org_query = org_query.filter(Organization.name.ilike(f"%{search}%"))
        org_total = org_query.scalar() or 0

    total = people_total + org_total
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1

    # Limit combined list to the requested page after global sort.
    contacts = contacts[offset:offset + per_page]

    # Align "total" stats with table contents when orgs are included.
    stats_total = leads_count + contacts_count + customers_count + subscribers_count
    if entity_type != "person":
        stats_total += orgs_count

    context = _contacts_base_context(request, db, "contacts")
    context.update({
        "contacts": contacts,
        "search": search or "",
        "status": status or "",
        "entity_type": entity_type or "",
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "stats": {
            "leads": leads_count,
            "contacts": contacts_count,
            "customers": customers_count,
            "subscribers": subscribers_count,
            "organizations": orgs_count,
            "total": stats_total,
        },
    })
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
    subscriber_type: Optional[str] = Form("person"),
    account_status: Optional[str] = Form("active"),
    db: Session = Depends(get_db),
):
    """Convert a lead/contact/customer person to a subscriber and create an account."""
    from app.schemas.subscriber import SubscriberAccountCreate, SubscriberCreate
    # TODO: person service removed
# from app.services.person import InvalidTransitionError
    from app.models.subscriber import SubscriberAccount, AccountStatus
    from app.services.common import validate_enum

    person = db.get(Subscriber, person_id)
    if not person:
        raise HTTPException(status_code=404, detail="Subscriber not found")
    # Log unsupported subscriber_type without changing behavior.
    if subscriber_type and subscriber_type != "person":
        logger.info(
            "Unsupported subscriber_type",
            extra={"subscriber_type": subscriber_type, "person_id": str(person.id)},
        )

    # Ensure party status can reach subscriber
    if person.party_status is None:
        person.party_status = PartyStatus.customer
        db.commit()
        db.refresh(person)
    elif person.party_status in (PartyStatus.lead, PartyStatus.contact):
        try:
            people_service.transition_status(db, str(person.id), PartyStatus.customer)
        except InvalidTransitionError as exc:
            logger.warning(
                "Customer transition failed",
                exc_info=exc,
                extra={"person_id": str(person.id)},
            )
            pass

    subscriber = (
        db.query(Subscriber)
        .filter(Subscriber.person_id == person.id)
        .first()
    )
    if not subscriber:
        subscriber = subscriber_service.subscribers.create(
            db=db,
            payload=SubscriberCreate(person_id=person.id),
        )

    existing_account = (
        db.query(SubscriberAccount)
        .filter(SubscriberAccount.subscriber_id == subscriber.id)
        .first()
    )
    if not existing_account:
        status_value = account_status or "active"
        try:
            status_enum = validate_enum(status_value, AccountStatus, "status")
        except Exception:
            status_enum = AccountStatus.active
        subscriber_service.accounts.create(
            db=db,
            payload=SubscriberAccountCreate(
                subscriber_id=subscriber.id,
                status=status_enum,
            ),
        )

    # Final status: subscriber if possible
    if person.party_status != PartyStatus.subscriber:
        try:
            people_service.transition_status(db, str(person.id), PartyStatus.subscriber)
        except InvalidTransitionError as exc:
            logger.warning(
                "Subscriber transition failed",
                exc_info=exc,
                extra={"person_id": str(person.id)},
            )
            pass

    missing_email = not bool(person.email)
    redirect_url = f"/admin/subscribers/{subscriber.id}"
    if missing_email:
        redirect_url = f"{redirect_url}?missing_email=1"
    return RedirectResponse(url=redirect_url, status_code=303)


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:read"))])
def customers_list(
    request: Request,
    search: Optional[str] = None,
    customer_type: Optional[str] = None,  # 'person' or 'organization'
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List all customers (people and organizations) with search and filtering."""
    offset = (page - 1) * per_page
    list_limit = per_page if customer_type else (offset + per_page)
    list_offset = offset if customer_type else 0

    customers = []
    total = 0

    if customer_type != "organization":
        # Only show people that are linked to a subscriber record.
        people_query = (
            db.query(Subscriber)
            .join(Subscriber, Subscriber.person_id == Subscriber.id)
        )
        if search:
            people_query = people_query.filter(Subscriber.email.ilike(f"%{search}%"))
        people = (
            people_query
            .order_by(Subscriber.created_at.desc())
            .limit(list_limit)
            .offset(list_offset)
            .all()
        )
        for p in people:
            customers.append({
                "id": str(p.id),
                "type": "person",
                "name": f"{p.first_name} {p.last_name}",
                "email": p.email,
                "phone": p.phone,
                "is_active": p.is_active,
                "created_at": p.created_at,
                "raw": p,
            })

    if customer_type != "person":
        # Get organizations
        orgs = subscriber_service.organizations.list(
            db=db,
            name=search if search else None,
            order_by="name",
            order_dir="asc",
            limit=list_limit,
            offset=list_offset,
        )
        for o in orgs:
            customers.append({
                "id": str(o.id),
                "type": "organization",
                "name": o.name,
                "email": getattr(o, "email", None),
                "phone": getattr(o, "phone", None),
                "is_active": getattr(o, "is_active", True),
                "created_at": o.created_at,
                "raw": o,
            })

    # Sort combined list by created_at desc
    customers.sort(key=lambda x: x["created_at"] or "", reverse=True)

    # Get total counts for pagination
    people_total = 0
    org_total = 0

    if customer_type != "organization":
        people_query = (
            db.query(func.count(Subscriber.id))
            .select_from(Subscriber)
            .join(Subscriber, Subscriber.person_id == Subscriber.id)
        )
        if search:
            people_query = people_query.filter(Subscriber.email.ilike(f"%{search}%"))
        people_total = people_query.scalar() or 0

    if customer_type != "person":
        org_query = db.query(func.count(Organization.id))
        if search:
            org_query = org_query.filter(Organization.name.ilike(f"%{search}%"))
        org_total = org_query.scalar() or 0

    total = people_total + org_total
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1

    # Apply pagination to combined list
    if not customer_type:
        customers = customers[offset:offset + per_page]

    # Stats
    stats = {
        "total_customers": total,
        "total_people": people_total,
        "total_organizations": org_total,
    }

    # Check if this is an HTMX request for table body only
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "admin/customers/_table.html",
            {
                "request": request,
                "customers": customers,
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": total_pages,
                "search": search,
                "customer_type": customer_type,
            },
        )

    # Get sidebar stats and current user
    from app.web.admin import get_sidebar_stats, get_current_user
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/customers/index.html",
        {
            "request": request,
            "customers": customers,
            "stats": stats,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "search": search,
            "customer_type": customer_type,
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
    from app.web.admin import get_sidebar_stats, get_current_user
    from app.services.smart_defaults import SmartDefaultsService

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
        customer_type = data.get("customer_type", "person")

        if customer_type == "person":
            # TODO: PersonCreate schema removed - use subscriber schemas
# from app.schemas.person import PersonCreate
            from app.schemas.subscriber import SubscriberAccountCreate, SubscriberCreate

            # Ingestion metadata contract stored in metadata_.ingest:
            # - source: string identifier of entry point
            # - received_at: ISO 8601 datetime when payload is received
            # - raw: original payload snapshot (pre-normalization)
            # - cleaning_version: cleaning pipeline version tag (default "v1")
            ingest_metadata = None
            existing_metadata = data.get("metadata")
            if isinstance(existing_metadata, dict) and existing_metadata.get("ingest"):
                ingest_metadata = existing_metadata
            else:
                ingest_metadata = existing_metadata if isinstance(existing_metadata, dict) else {}
                ingest_metadata["ingest"] = {
                    "source": "admin/customers/wizard",
                    "received_at": datetime.now(timezone.utc).isoformat(),
                    "raw": dict(data),
                    "cleaning_version": "v1",
                }

            email = data.get("email", "").strip()
            if not email:
                raise ValueError("email is required")

            existing = (
                db.query(Subscriber)
                .filter(func.lower(Subscriber.email) == email.lower())
                .first()
            )
            if existing:
                raise ValueError(f"A customer with email {email} already exists.")

            person_data = PersonCreate(
                first_name=data.get("first_name", "").strip(),
                last_name=data.get("last_name", "").strip(),
                display_name=data.get("display_name", "").strip() or None,
                email=email,
                phone=data.get("phone", "").strip() or None,
                date_of_birth=data.get("date_of_birth") or None,
                gender=data.get("gender", "unknown"),
                address_line1=data.get("address_line1", "").strip() or None,
                address_line2=data.get("address_line2", "").strip() or None,
                city=data.get("city", "").strip() or None,
                region=data.get("region", "").strip() or None,
                postal_code=data.get("postal_code", "").strip() or None,
                country_code=data.get("country_code", "").strip() or None,
                is_active=data.get("is_active", True),
                status=data.get("status", "active"),
                notes=data.get("notes", "").strip() or None,
                metadata_=ingest_metadata,
            )

            person = people_service.create(db=db, person_data=person_data)

            # Create subscriber record
            subscriber_data = SubscriberCreate(
                person_id=person.id,
                organization_id=None,
            )
            subscriber = subscriber_service.create_subscriber(db, subscriber_data)

            # Create default account
            account_data = SubscriberAccountCreate(
                account_number=None,  # Will be auto-generated
            )
            subscriber_service.create_account(db, subscriber.id, account_data)

            return {"success": True, "redirect": f"/admin/customers/{person.id}"}

        elif customer_type == "organization":
            from app.schemas.subscriber import OrganizationCreate, SubscriberAccountCreate, SubscriberCreate

            org_name = data.get("name", "").strip()
            if not org_name:
                raise ValueError("Organization name is required")

            existing = (
                db.query(Organization)
                .filter(func.lower(Organization.name) == org_name.lower())
                .first()
            )
            if existing:
                raise ValueError(f"An organization with name {org_name} already exists.")

            org_data = OrganizationCreate(
                name=org_name,
                legal_name=data.get("legal_name", "").strip() or None,
                tax_id=data.get("tax_id", "").strip() or None,
                domain=data.get("domain", "").strip() or None,
                website=data.get("website", "").strip() or None,
                address_line1=data.get("address_line1", "").strip() or None,
                address_line2=data.get("address_line2", "").strip() or None,
                city=data.get("city", "").strip() or None,
                region=data.get("region", "").strip() or None,
                postal_code=data.get("postal_code", "").strip() or None,
                country_code=data.get("country_code", "").strip() or None,
                notes=data.get("notes", "").strip() or None,
            )

            org = subscriber_service.create_organization(db, org_data)

            # Create subscriber record
            subscriber_data = SubscriberCreate(
                person_id=None,
                organization_id=org.id,
            )
            subscriber = subscriber_service.create_subscriber(db, subscriber_data)

            # Create default account
            account_data = SubscriberAccountCreate(
                account_number=None,
            )
            subscriber_service.create_account(db, subscriber.id, account_data)

            # Create contacts if provided
            contacts = data.get("contacts", [])
            for contact_data in contacts:
                first_name = contact_data.get("first_name", "").strip()
                last_name = contact_data.get("last_name", "").strip()
                if not first_name or not last_name:
                    continue

                # TODO: PersonCreate schema removed - use subscriber schemas
# from app.schemas.person import PersonCreate
                contact_person = PersonCreate(
                    first_name=first_name,
                    last_name=last_name,
                    email=contact_data.get("email", "").strip() or None,
                    phone=contact_data.get("phone", "").strip() or None,
                    is_active=True,
                    status="active",
                )
                person = people_service.create(db=db, person_data=contact_person)

                # Link to organization
                from app.models.subscriber import OrganizationContact
                org_contact = OrganizationContact(
                    organization_id=org.id,
                    person_id=person.id,
                    role=contact_data.get("role", "primary"),
                    title=contact_data.get("title", "").strip() or None,
                    is_primary=contact_data.get("is_primary", False),
                )
                db.add(org_contact)
            db.commit()

            return {"success": True, "redirect": f"/admin/customers/{org.id}?type=organization"}

        else:
            raise ValueError("Invalid customer type")

    except ValueError as exc:
        return {"success": False, "message": str(exc)}
    except IntegrityError:
        db.rollback()
        return {"success": False, "message": "A customer with this information already exists."}
    except Exception as exc:
        db.rollback()
        return {"success": False, "message": f"An error occurred: {str(exc)}"}


@router.get("/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:write"))])
def customer_new(
    request: Request,
    type: Optional[str] = "person",
    db: Session = Depends(get_db),
):
    """New customer form."""
    from app.web.admin import get_sidebar_stats, get_current_user
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
    first_name: Optional[str] = Form(None),
    last_name: Optional[str] = Form(None),
    display_name: Optional[str] = Form(None),
    avatar_url: Optional[str] = Form(None),
    bio: Optional[str] = Form(None),
    # Organization fields
    name: Optional[str] = Form(None),
    legal_name: Optional[str] = Form(None),
    tax_id: Optional[str] = Form(None),
    domain: Optional[str] = Form(None),
    website: Optional[str] = Form(None),
    org_notes: Optional[str] = Form(None),
    # Common fields
    email: Optional[str] = Form(None),
    email_verified: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    date_of_birth: Optional[str] = Form(None),
    gender: Optional[str] = Form(None),
    preferred_contact_method: Optional[str] = Form(None),
    locale: Optional[str] = Form(None),
    timezone: Optional[str] = Form(None),
    address_line1: Optional[str] = Form(None),
    address_line2: Optional[str] = Form(None),
    city: Optional[str] = Form(None),
    region: Optional[str] = Form(None),
    postal_code: Optional[str] = Form(None),
    country_code: Optional[str] = Form(None),
    status: Optional[str] = Form(None),
    is_active: Optional[str] = Form(None),
    marketing_opt_in: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    account_start_date: Optional[str] = Form(None),
    org_account_start_date: Optional[str] = Form(None),
    metadata: Optional[str] = Form(None),
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
        def _parse_contact_rows() -> list[dict]:
            fields = [
                contact_first_name,
                contact_last_name,
                contact_title,
                contact_role,
                contact_email,
                contact_phone,
                contact_is_primary,
            ]
            max_len = max((len(field) for field in fields), default=0)
            rows: list[dict] = []
            for idx in range(max_len):
                first = contact_first_name[idx].strip() if idx < len(contact_first_name) and contact_first_name[idx] else ""
                last = contact_last_name[idx].strip() if idx < len(contact_last_name) and contact_last_name[idx] else ""
                title_value = contact_title[idx].strip() if idx < len(contact_title) and contact_title[idx] else None
                email_value = contact_email[idx].strip() if idx < len(contact_email) and contact_email[idx] else None
                phone_value = contact_phone[idx].strip() if idx < len(contact_phone) and contact_phone[idx] else None
                is_primary_value = (
                    contact_is_primary[idx].strip().lower() == "true"
                    if idx < len(contact_is_primary) and contact_is_primary[idx]
                    else False
                )

                if not any([first, last, title_value, email_value, phone_value, is_primary_value]):
                    continue
                if not first or not last:
                    raise ValueError("Contact first and last name are required.")
                role_value = contact_role[idx].strip() if idx < len(contact_role) and contact_role[idx] else "primary"
                rows.append(
                    {
                        "first_name": first,
                        "last_name": last,
                        "title": title_value,
                        "role": role_value,
                        "email": email_value,
                        "phone": phone_value,
                        "is_primary": is_primary_value,
                    }
                )
            return rows

        contact_rows = _parse_contact_rows()
        if customer_type not in ("person", "organization"):
            raise ValueError("customer_type must be person or organization")
        if customer_type == "person":
            # TODO: PersonCreate schema removed - use subscriber schemas
# from app.schemas.person import PersonCreate
            from app.schemas.subscriber import SubscriberAccountCreate, SubscriberCreate
            normalized_email = email.strip() if email else None
            if not normalized_email:
                raise ValueError("email is required")
            existing = (
                db.query(Subscriber)
                .filter(func.lower(Subscriber.email) == normalized_email.lower())
                .first()
            )
            if existing:
                raise ValueError(f"A customer with email {normalized_email} already exists.")
            data = PersonCreate(
                first_name=first_name,
                last_name=last_name,
                display_name=display_name.strip() if display_name else None,
                avatar_url=avatar_url.strip() if avatar_url else None,
                bio=bio.strip() if bio else None,
                email=normalized_email,
                email_verified=email_verified == "true",
                phone=phone if phone else None,
                date_of_birth=date_of_birth or None,
                gender=gender or "unknown",
                preferred_contact_method=preferred_contact_method or None,
                locale=locale.strip() if locale else None,
                timezone=timezone.strip() if timezone else None,
                address_line1=address_line1.strip() if address_line1 else None,
                address_line2=address_line2.strip() if address_line2 else None,
                city=city.strip() if city else None,
                region=region.strip() if region else None,
                postal_code=postal_code.strip() if postal_code else None,
                country_code=country_code.strip() if country_code else None,
                status=status or "active",
                is_active=is_active == "true",
                marketing_opt_in=marketing_opt_in == "true",
                notes=notes.strip() if notes else None,
                metadata_=_parse_json(metadata, "metadata"),
            )
            customer = people_service.create(db=db, payload=data)
            if contact_rows:
                subscriber = subscriber_service.subscribers.create(
                    db,
                    SubscriberCreate(
                        person_id=customer.id,
                        is_active=True,
                        account_start_date=_parse_date(account_start_date),
                    ),
                )
                account = subscriber_service.accounts.create(
                    db,
                    SubscriberAccountCreate(subscriber_id=subscriber.id),
                )
                _create_account_roles_from_rows(db, str(account.id), contact_rows)
        else:
            from app.schemas.subscriber import OrganizationCreate, SubscriberAccountCreate, SubscriberCreate
            data = OrganizationCreate(
                name=name,
                legal_name=legal_name.strip() if legal_name else None,
                tax_id=tax_id.strip() if tax_id else None,
                domain=domain.strip() if domain else None,
                website=website.strip() if website else None,
                notes=org_notes.strip() if org_notes else None,
            )
            customer = subscriber_service.organizations.create(db=db, payload=data)
            if contact_rows:
                # Create a Subscriber from the first contact row
                first_contact = contact_rows[0]
                primary_person = people_service.create(
                    db=db,
                    payload=PersonCreate(
                        first_name=first_contact["first_name"],
                        last_name=first_contact["last_name"],
                        email=first_contact["email"] or f"org-{customer.id}@placeholder.local",
                        phone=first_contact["phone"] or None,
                        organization_id=customer.id,
                    ),
                )
                subscriber = subscriber_service.subscribers.create(
                    db,
                    SubscriberCreate(
                        person_id=primary_person.id,
                        is_active=True,
                        account_start_date=_parse_date(org_account_start_date),
                    ),
                )
                account = subscriber_service.accounts.create(
                    db,
                    SubscriberAccountCreate(subscriber_id=subscriber.id),
                )
                _create_account_roles_from_rows(
                    db, str(account.id), contact_rows, organization_id=str(customer.id)
                )

        return RedirectResponse(
            url=f"/admin/customers/{customer_type}/{customer.id}",
            status_code=303,
        )
    except HTTPException:
        raise
    except Exception as e:
        from app.web.admin import get_sidebar_stats, get_current_user
        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        contact_rows = []
        try:
            for idx in range(
                max(
                    len(contact_first_name),
                    len(contact_last_name),
                    len(contact_title),
                    len(contact_role),
                    len(contact_email),
                    len(contact_phone),
                    len(contact_is_primary),
                )
            ):
                contact_rows.append(
                    {
                        "first_name": contact_first_name[idx] if idx < len(contact_first_name) else "",
                        "last_name": contact_last_name[idx] if idx < len(contact_last_name) else "",
                        "title": contact_title[idx] if idx < len(contact_title) else "",
                        "role": contact_role[idx] if idx < len(contact_role) else "primary",
                        "email": contact_email[idx] if idx < len(contact_email) else "",
                        "phone": contact_phone[idx] if idx < len(contact_phone) else "",
                        "is_primary": (
                            contact_is_primary[idx].strip().lower() == "true"
                            if idx < len(contact_is_primary) and contact_is_primary[idx]
                            else False
                        ),
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
        customer = people_service.get(db=db, person_id=customer_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Customer not found"},
            status_code=404,
        )

    # Get person's subscriber records
    subscribers = []
    addresses = []
    contacts = []
    accounts = []
    try:
        from uuid import UUID
        subscribers = subscriber_service.subscribers.list(
            db=db,
            person_id=UUID(customer_id),
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
    except Exception:
        pass

    # Get addresses and accounts for all subscribers
    invoices = []
    payments = []
    balance_due = 0
    for sub in subscribers:
        # Get addresses for this subscriber
        try:
            sub_addresses = subscriber_service.addresses.list(
                db=db,
                subscriber_id=str(sub.id),
                account_id=None,
                order_by="created_at",
                order_dir="desc",
                limit=50,
                offset=0,
            )
            addresses.extend(sub_addresses)
        except Exception:
            pass

        # Get accounts and contacts for this subscriber
        try:
            sub_accounts = subscriber_service.accounts.list(
                db=db,
                subscriber_id=str(sub.id),
                reseller_id=None,
                order_by="created_at",
                order_dir="desc",
                limit=10,
                offset=0,
            )
            accounts.extend(sub_accounts)
            for account in sub_accounts:
                # Get contacts for this account
                try:
                    acc_contacts = subscriber_service.account_roles.list(
                        db=db,
                        account_id=str(account.id),
                        person_id=None,
                        order_by="created_at",
                        order_dir="desc",
                        limit=50,
                        offset=0,
                    )
                    contacts.extend(acc_contacts)
                except Exception:
                    pass
        except Exception:
            pass

    # Include accounts linked via account roles (e.g., contact/primary roles)
    try:
        role_links = subscriber_service.account_roles.list(
            db=db,
            account_id=None,
            person_id=str(customer_id),
            order_by="created_at",
            order_dir="desc",
            limit=200,
            offset=0,
        )
        for role in role_links:
            try:
                account = subscriber_service.accounts.get(db=db, account_id=str(role.account_id))
                accounts.append(account)
            except Exception:
                continue
    except Exception:
        pass

    contacts = [_format_account_role(role) for role in contacts]

    accounts = _dedupe_accounts(accounts)
    if accounts:
        subscriber_ids = {str(sub.id) for sub in subscribers}
        for account in accounts:
            sub = getattr(account, "subscriber", None)
            if sub and str(sub.id) not in subscriber_ids:
                subscribers.append(sub)
                subscriber_ids.add(str(sub.id))
    subscriptions = _list_subscriptions_for_accounts(db, accounts)
    account_lookup = {str(account.id): account for account in accounts}
    account_ids = [account.id for account in accounts]

    if account_ids:
        invoices = (
            db.query(Invoice)
            .filter(Invoice.account_id.in_(account_ids))
            .order_by(Invoice.created_at.desc())
            .limit(10)
            .all()
        )
        payments = (
            db.query(Payment)
            .filter(Payment.account_id.in_(account_ids))
            .order_by(Payment.created_at.desc())
            .limit(10)
            .all()
        )

    def get_status(obj):
        status = getattr(obj, "status", "")
        return status.value if hasattr(status, "value") else str(status)
    balance_due = sum(
        float(getattr(inv, "balance_due", 0) or 0)
        for inv in invoices
        if get_status(inv) in ("issued", "partially_paid", "overdue")
    )
    active_subscriptions = sum(1 for sub in subscriptions if get_status(sub) == "active")
    monthly_recurring = sum(
        float(getattr(sub, "unit_price", 0) or 0)
        for sub in subscriptions
        if get_status(sub) == "active"
    )
    total_invoiced = 0
    total_paid = 0
    overdue_invoices = 0
    last_payment = None
    last_invoice = None
    if account_ids:
        total_invoiced = (
            db.query(func.coalesce(func.sum(Invoice.total), 0))
            .filter(Invoice.account_id.in_(account_ids))
            .scalar()
            or 0
        )
        total_paid = (
            db.query(func.coalesce(func.sum(Payment.amount), 0))
            .filter(Payment.account_id.in_(account_ids))
            .filter(Payment.status == PaymentStatus.succeeded)
            .scalar()
            or 0
        )
        overdue_invoices = (
            db.query(func.count(Invoice.id))
            .filter(Invoice.account_id.in_(account_ids))
            .filter(Invoice.status == InvoiceStatus.overdue)
            .scalar()
            or 0
        )
        last_payment = (
            db.query(Payment)
            .filter(Payment.account_id.in_(account_ids))
            .filter(Payment.status == PaymentStatus.succeeded)
            .order_by(func.coalesce(Payment.paid_at, Payment.created_at).desc())
            .first()
        )
        last_invoice = (
            db.query(Invoice)
            .filter(Invoice.account_id.in_(account_ids))
            .order_by(func.coalesce(Invoice.issued_at, Invoice.created_at).desc())
            .first()
        )

    if not addresses and (customer.address_line1 or "").strip():
        from types import SimpleNamespace
        customer_meta = getattr(customer, "metadata_", None) or {}
        customer_lat = getattr(customer, "latitude", None)
        customer_lng = getattr(customer, "longitude", None)
        def _clean_value(value):
            if isinstance(value, str):
                trimmed = value.strip()
                return None if not trimmed or trimmed.lower() == "none" else trimmed
            return value
        address_line1 = (customer.address_line1 or "").strip()
        address_line2 = _clean_value(getattr(customer, "address_line2", None))
        city = _clean_value(getattr(customer, "city", None))
        region = _clean_value(getattr(customer, "region", None))
        postal_code = _clean_value(getattr(customer, "postal_code", None))
        country_code = _clean_value(getattr(customer, "country_code", None))
        if customer_lat is None:
            customer_lat = customer_meta.get("latitude")
        if customer_lng is None:
            customer_lng = customer_meta.get("longitude")
        if customer_lat is None or customer_lng is None:
            try:
                from app.schemas.geocoding import GeocodePreviewRequest
                from app.services import geocoding as geocoding_service

                payload = GeocodePreviewRequest(
                    address_line1=address_line1,
                    address_line2=address_line2,
                    city=city,
                    region=region,
                    postal_code=postal_code,
                    country_code=country_code,
                    limit=1,
                )
                results = geocoding_service.geocode_preview_from_request(db, payload)
                if results:
                    first = results[0] or {}
                    lat_value = first.get("latitude")
                    lng_value = first.get("longitude")
                    if lat_value is not None and lng_value is not None:
                        customer_lat = float(lat_value)
                        customer_lng = float(lng_value)
                        if getattr(customer, "metadata_", None) is None:
                            customer.metadata_ = {}
                        if isinstance(customer.metadata_, dict):
                            customer.metadata_["latitude"] = customer_lat
                            customer.metadata_["longitude"] = customer_lng
                            try:
                                db.add(customer)
                                db.commit()
                            except Exception:
                                db.rollback()
            except Exception:
                pass
        addresses = [
            SimpleNamespace(
                id=None,
                is_primary=True,
                address_line1=address_line1,
                address_line2=address_line2,
                city=city,
                region=region,
                postal_code=postal_code,
                country_code=country_code,
                latitude=customer_lat,
                longitude=customer_lng,
                created_at=None,
            )
        ]

    primary_address = next(
        (a for a in addresses if getattr(a, "is_primary", False) and (a.address_line1 or "").strip()),
        next(
            (a for a in addresses if getattr(a, "is_primary", False)),
            next((a for a in addresses if (a.address_line1 or "").strip()), addresses[0] if addresses else None),
        ),
    )
    map_data = None
    geocode_target = None
    if primary_address and (primary_address.address_line1 or "").strip():
        if primary_address.latitude is not None and primary_address.longitude is not None:
            map_data = {
                "center": [primary_address.latitude, primary_address.longitude],
                "geojson": {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "Point",
                                "coordinates": [primary_address.longitude, primary_address.latitude],
                            },
                            "properties": {
                                "type": "customer",
                                "name": f"{customer.first_name or ''} {customer.last_name or ''}".strip(),
                                "address": primary_address.address_line1,
                            },
                        }
                    ],
                },
            }
        else:
            geocode_target = {
                "id": str(primary_address.id),
                "address_line1": primary_address.address_line1,
                "address_line2": primary_address.address_line2,
                "city": primary_address.city,
                "region": primary_address.region,
                "postal_code": primary_address.postal_code,
                "country_code": primary_address.country_code,
                "payload": {
                    "address_line1": primary_address.address_line1,
                    "address_line2": primary_address.address_line2 or "",
                    "city": primary_address.city or "",
                    "region": primary_address.region or "",
                    "postal_code": primary_address.postal_code or "",
                    "country_code": primary_address.country_code or "",
                },
            }

    stats = {
        "total_subscribers": len(subscribers),
        "total_subscriptions": len(subscriptions),
        "active_subscriptions": active_subscriptions,
        "balance_due": balance_due,
        "total_addresses": len(addresses),
        "total_contacts": len(contacts),
    }
    financials = {
        "total_invoiced": total_invoiced,
        "total_paid": total_paid,
        "overdue_invoices": overdue_invoices,
        "last_payment": last_payment,
        "last_invoice": last_invoice,
        "monthly_recurring": monthly_recurring,
    }
    from uuid import UUID
    person_uuid = UUID(customer_id)
    active_subscribers = (
        db.query(Subscriber)
        .filter(Subscriber.person_id == person_uuid)
        .filter(Subscriber.is_active.is_(True))
        .count()
    )
    total_subscribers = (
        db.query(Subscriber)
        .filter(Subscriber.person_id == person_uuid)
        .count()
    )

    # Get notifications sent to this customer
    notifications = []
    try:
        recipients = []
        if customer.email:
            recipients.append(customer.email)
        if customer.phone:
            recipients.append(customer.phone)
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
            notifications = [n for n in all_notifications if n.recipient in recipients][:5]
    except Exception:
        pass

    audit_events = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type="person",
        entity_id=str(customer_id),
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
            for person in db.query(Subscriber).filter(Subscriber.id.in_(actor_ids)).all()
        }
    activity_items = []
    for event in audit_events:
        actor = people.get(str(event.actor_id)) if getattr(event, "actor_id", None) else None
        actor_name = f"{actor.first_name} {actor.last_name}".strip() if actor else "System"
        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes, max_items=2)
        description_parts = [actor_name]
        if change_summary:
            description_parts.append(change_summary)
        activity_items.append(
            {
                "type": "audit",
                "title": (event.action or "Activity").replace("_", " ").title(),
                "description": " · ".join(description_parts),
                "timestamp": event.occurred_at,
            }
        )

    from app.web.admin import get_sidebar_stats, get_current_user
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/customers/detail.html",
        {
            "request": request,
            "customer": customer,
            "customer_type": "person",
            "customer_name": f"{customer.first_name} {customer.last_name}",
            "subscribers": subscribers,
            "accounts": accounts,
            "subscriptions": subscriptions,
            "account_lookup": account_lookup,
            "addresses": addresses,
            "primary_address": primary_address,
            "map_data": map_data,
            "geocode_target": geocode_target,
            "contacts": contacts,
            "invoices": invoices,
            "payments": payments,
            "notifications": notifications,
            "stats": stats,
            "financials": financials,
            "has_active_subscribers": active_subscribers > 0,
            "has_any_subscribers": total_subscribers > 0,
            "activity_items": activity_items,
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
        customer = subscriber_service.organizations.get(db=db, organization_id=customer_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Organization not found"},
            status_code=404,
        )

    # Get organization's subscriber records
    subscribers = []
    addresses = []
    contacts = []
    accounts = []
    try:
        from uuid import UUID
        org_uuid = UUID(customer_id)
        subscribers = (
            db.query(Subscriber)
            # TODO: Person model removed - Subscriber no longer has person_id FK
# .join(Person, Subscriber.person_id == Subscriber.id)
            .filter(Subscriber.organization_id == org_uuid)
            .order_by(Subscriber.created_at.desc())
            .limit(10)
            .all()
        )
    except Exception:
        pass

    # Get addresses and accounts for all subscribers
    invoices = []
    payments = []
    balance_due = 0
    for sub in subscribers:
        # Get addresses for this subscriber
        try:
            sub_addresses = subscriber_service.addresses.list(
                db=db,
                subscriber_id=str(sub.id),
                account_id=None,
                order_by="created_at",
                order_dir="desc",
                limit=50,
                offset=0,
            )
            addresses.extend(sub_addresses)
        except Exception:
            pass

        # Get accounts and contacts for this subscriber
        try:
            sub_accounts = subscriber_service.accounts.list(
                db=db,
                subscriber_id=str(sub.id),
                reseller_id=None,
                order_by="created_at",
                order_dir="desc",
                limit=10,
                offset=0,
            )
            accounts.extend(sub_accounts)
            for account in sub_accounts:
                # Get contacts for this account
                try:
                    acc_contacts = subscriber_service.account_roles.list(
                        db=db,
                        account_id=str(account.id),
                        person_id=None,
                        order_by="created_at",
                        order_dir="desc",
                        limit=50,
                        offset=0,
                    )
                    contacts.extend(acc_contacts)
                except Exception:
                    pass
        except Exception:
            pass

    contacts = [_format_account_role(role) for role in contacts]

    accounts = _dedupe_accounts(accounts)
    subscriptions = _list_subscriptions_for_accounts(db, accounts)
    account_lookup = {str(account.id): account for account in accounts}
    account_ids = [account.id for account in accounts]

    if account_ids:
        invoices = (
            db.query(Invoice)
            .filter(Invoice.account_id.in_(account_ids))
            .order_by(Invoice.created_at.desc())
            .limit(10)
            .all()
        )
        payments = (
            db.query(Payment)
            .filter(Payment.account_id.in_(account_ids))
            .order_by(Payment.created_at.desc())
            .limit(10)
            .all()
        )

    def get_status(obj):
        status = getattr(obj, "status", "")
        return status.value if hasattr(status, "value") else str(status)
    balance_due = sum(
        float(getattr(inv, "balance_due", 0) or 0)
        for inv in invoices
        if get_status(inv) in ("issued", "partially_paid", "overdue")
    )
    active_subscriptions = sum(1 for sub in subscriptions if get_status(sub) == "active")
    monthly_recurring = sum(
        float(getattr(sub, "unit_price", 0) or 0)
        for sub in subscriptions
        if get_status(sub) == "active"
    )
    total_invoiced = 0
    total_paid = 0
    overdue_invoices = 0
    last_payment = None
    last_invoice = None
    if account_ids:
        total_invoiced = (
            db.query(func.coalesce(func.sum(Invoice.total), 0))
            .filter(Invoice.account_id.in_(account_ids))
            .scalar()
            or 0
        )
        total_paid = (
            db.query(func.coalesce(func.sum(Payment.amount), 0))
            .filter(Payment.account_id.in_(account_ids))
            .filter(Payment.status == PaymentStatus.succeeded)
            .scalar()
            or 0
        )
        overdue_invoices = (
            db.query(func.count(Invoice.id))
            .filter(Invoice.account_id.in_(account_ids))
            .filter(Invoice.status == InvoiceStatus.overdue)
            .scalar()
            or 0
        )
        last_payment = (
            db.query(Payment)
            .filter(Payment.account_id.in_(account_ids))
            .filter(Payment.status == PaymentStatus.succeeded)
            .order_by(func.coalesce(Payment.paid_at, Payment.created_at).desc())
            .first()
        )
        last_invoice = (
            db.query(Invoice)
            .filter(Invoice.account_id.in_(account_ids))
            .order_by(func.coalesce(Invoice.issued_at, Invoice.created_at).desc())
            .first()
        )

    primary_address = next((a for a in addresses if getattr(a, "is_primary", False)), addresses[0] if addresses else None)
    map_data = None
    geocode_target = None
    if primary_address and primary_address.address_line1:
        if primary_address.latitude and primary_address.longitude:
            map_data = {
                "center": [primary_address.latitude, primary_address.longitude],
                "geojson": {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "Point",
                                "coordinates": [primary_address.longitude, primary_address.latitude],
                            },
                            "properties": {
                                "type": "customer",
                                "name": customer.name,
                                "address": primary_address.address_line1,
                            },
                        }
                    ],
                },
            }
        else:
            geocode_target = {
                "id": str(primary_address.id),
                "address_line1": primary_address.address_line1,
                "address_line2": primary_address.address_line2,
                "city": primary_address.city,
                "region": primary_address.region,
                "postal_code": primary_address.postal_code,
                "country_code": primary_address.country_code,
                "payload": {
                    "address_line1": primary_address.address_line1,
                    "address_line2": primary_address.address_line2,
                    "city": primary_address.city,
                    "region": primary_address.region,
                    "postal_code": primary_address.postal_code,
                    "country_code": primary_address.country_code,
                },
            }

    stats = {
        "total_subscribers": len(subscribers),
        "total_subscriptions": len(subscriptions),
        "active_subscriptions": active_subscriptions,
        "balance_due": balance_due,
        "total_addresses": len(addresses),
        "total_contacts": len(contacts),
    }
    financials = {
        "total_invoiced": total_invoiced,
        "total_paid": total_paid,
        "overdue_invoices": overdue_invoices,
        "last_payment": last_payment,
        "last_invoice": last_invoice,
        "monthly_recurring": monthly_recurring,
    }
    from uuid import UUID
    org_uuid = UUID(customer_id)
    active_subscribers = (
        db.query(Subscriber)
        # TODO: Person model removed - Subscriber no longer has person_id FK
# .join(Person, Subscriber.person_id == Subscriber.id)
        .filter(Subscriber.organization_id == org_uuid)
        .filter(Subscriber.is_active.is_(True))
        .count()
    )
    total_subscribers = (
        db.query(Subscriber)
        # TODO: Person model removed - Subscriber no longer has person_id FK
# .join(Person, Subscriber.person_id == Subscriber.id)
        .filter(Subscriber.organization_id == org_uuid)
        .count()
    )

    # Get notifications sent to organization contacts
    notifications = []
    try:
        recipients = []
        # Get contact emails/phones from organization's people
        org_people = db.query(Subscriber).filter(Subscriber.organization_id == org_uuid).limit(10).all()
        for person in org_people:
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
            notifications = [n for n in all_notifications if n.recipient in recipients][:5]
    except Exception:
        pass

    audit_events = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type="organization",
        entity_id=str(customer_id),
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
            for person in db.query(Subscriber).filter(Subscriber.id.in_(actor_ids)).all()
        }
    activity_items = []
    for event in audit_events:
        actor = people.get(str(event.actor_id)) if getattr(event, "actor_id", None) else None
        actor_name = f"{actor.first_name} {actor.last_name}".strip() if actor else "System"
        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes, max_items=2)
        description_parts = [actor_name]
        if change_summary:
            description_parts.append(change_summary)
        activity_items.append(
            {
                "type": "audit",
                "title": (event.action or "Activity").replace("_", " ").title(),
                "description": " · ".join(description_parts),
                "timestamp": event.occurred_at,
            }
        )

    from app.web.admin import get_sidebar_stats, get_current_user
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/customers/detail.html",
        {
            "request": request,
            "customer": customer,
            "customer_type": "organization",
            "customer_name": customer.name,
            "subscribers": subscribers,
            "accounts": accounts,
            "subscriptions": subscriptions,
            "account_lookup": account_lookup,
            "addresses": addresses,
            "primary_address": primary_address,
            "map_data": map_data,
            "geocode_target": geocode_target,
            "contacts": contacts,
            "invoices": invoices,
            "payments": payments,
            "notifications": notifications,
            "stats": stats,
            "financials": financials,
            "has_active_subscribers": active_subscribers > 0,
            "has_any_subscribers": total_subscribers > 0,
            "activity_items": activity_items,
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.post("/person/{customer_id}/impersonate", response_class=HTMLResponse)
def person_impersonate(
    request: Request,
    customer_id: str,
    account_id: str = Form(...),
    subscription_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    auth=Depends(require_permission("subscriber:impersonate")),
):
    """Impersonate a person customer and open the portal."""
    return _impersonate_customer(
        request=request,
        customer_type="person",
        customer_id=customer_id,
        account_id=account_id,
        subscription_id=subscription_id,
        auth=auth,
        db=db,
    )


@router.post("/organization/{customer_id}/impersonate", response_class=HTMLResponse)
def organization_impersonate(
    request: Request,
    customer_id: str,
    account_id: str = Form(...),
    subscription_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    auth=Depends(require_permission("subscriber:impersonate")),
):
    """Impersonate an organization customer and open the portal."""
    return _impersonate_customer(
        request=request,
        customer_type="organization",
        customer_id=customer_id,
        account_id=account_id,
        subscription_id=subscription_id,
        auth=auth,
        db=db,
    )


def _impersonate_customer(
    request: Request,
    customer_type: str,
    customer_id: str,
    account_id: str,
    subscription_id: Optional[str],
    auth: dict,
    db: Session,
):
    from app.services import catalog as catalog_service
    from app.services import customer_portal
    from app.schemas.audit import AuditEventCreate
    from app.models.audit import AuditActorType

    subscribers = []
    if customer_type == "person":
        from uuid import UUID
        subscribers = subscriber_service.subscribers.list(
            db=db,
            person_id=UUID(customer_id),
            order_by="created_at",
            order_dir="desc",
            limit=50,
            offset=0,
        )
    else:
        from uuid import UUID
        org_uuid = UUID(customer_id)
        subscribers = (
            db.query(Subscriber)
            # TODO: Person model removed - Subscriber no longer has person_id FK
# .join(Person, Subscriber.person_id == Subscriber.id)
            .filter(Subscriber.organization_id == org_uuid)
            .order_by(Subscriber.created_at.desc())
            .limit(50)
            .all()
        )

    accounts = []
    for sub in subscribers:
        try:
            sub_accounts = subscriber_service.accounts.list(
                db=db,
                subscriber_id=str(sub.id),
                reseller_id=None,
                order_by="created_at",
                order_dir="desc",
                limit=50,
                offset=0,
            )
            accounts.extend(sub_accounts)
        except Exception:
            continue

    accounts = _dedupe_accounts(accounts)
    account_lookup = {str(acc.id): acc for acc in accounts}
    selected_account = account_lookup.get(account_id)
    if not selected_account:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Subscriber account not found"},
            status_code=404,
        )

    selected_subscription_id = None
    if subscription_id:
        subscription = catalog_service.subscriptions.get(db=db, subscription_id=subscription_id)
        if str(getattr(subscription, "account_id", "")) != str(selected_account.id):
            return templates.TemplateResponse(
                "admin/errors/404.html",
                {"request": request, "message": "Subscription not found"},
                status_code=404,
            )
        selected_subscription_id = subscription.id
    else:
        active_subs = catalog_service.subscriptions.list(
            db=db,
            account_id=str(selected_account.id),
            offer_id=None,
            status="active",
            order_by="created_at",
            order_dir="desc",
            limit=1,
            offset=0,
        )
        if active_subs:
            selected_subscription_id = active_subs[0].id
        else:
            any_subs = catalog_service.subscriptions.list(
                db=db,
                account_id=str(selected_account.id),
                offer_id=None,
                status=None,
                order_by="created_at",
                order_dir="desc",
                limit=1,
                offset=0,
            )
            if any_subs:
                selected_subscription_id = any_subs[0].id

    session_token = customer_portal.create_customer_session(
        username=f"impersonate:{customer_type}:{customer_id}:{selected_account.id}",
        account_id=selected_account.id,
        subscriber_id=selected_account.subscriber_id,
        subscription_id=selected_subscription_id,
        return_to=f"/admin/customers/{customer_type}/{customer_id}",
    )

    actor_id_value = None
    if isinstance(auth, dict):
        actor_id_value = str(auth.get("person_id") or "") or None

    audit_payload = AuditEventCreate(
        actor_type=AuditActorType.user,
        actor_id=actor_id_value,
        action="impersonate",
        entity_type="subscriber_account",
        entity_id=str(selected_account.id),
        status_code=303,
        is_success=True,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata={
            "customer_type": customer_type,
            "customer_id": customer_id,
            "subscription_id": str(selected_subscription_id) if selected_subscription_id else None,
        },
    )
    audit_service.audit_events.create(db=db, payload=audit_payload)

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
        customer = people_service.get(db=db, person_id=customer_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Customer not found"},
            status_code=404,
        )

    from app.web.admin import get_sidebar_stats, get_current_user
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

    from app.web.admin import get_sidebar_stats, get_current_user
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
    display_name: Optional[str] = Form(None),
    avatar_url: Optional[str] = Form(None),
    bio: Optional[str] = Form(None),
    email: Optional[str] = Form(None),
    email_verified: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    date_of_birth: Optional[str] = Form(None),
    gender: Optional[str] = Form(None),
    preferred_contact_method: Optional[str] = Form(None),
    locale: Optional[str] = Form(None),
    timezone: Optional[str] = Form(None),
    address_line1: Optional[str] = Form(None),
    address_line2: Optional[str] = Form(None),
    city: Optional[str] = Form(None),
    region: Optional[str] = Form(None),
    postal_code: Optional[str] = Form(None),
    country_code: Optional[str] = Form(None),
    status: Optional[str] = Form(None),
    is_active: Optional[str] = Form(None),
    marketing_opt_in: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    account_start_date: Optional[str] = Form(None),
    metadata: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Update a person."""
    try:
        # TODO: PersonUpdate schema removed - use subscriber schemas
# from app.schemas.person import PersonUpdate
        before = people_service.get(db=db, person_id=customer_id)
        active = is_active == "true"
        data = PersonUpdate(
            first_name=first_name,
            last_name=last_name,
            display_name=display_name.strip() if display_name else None,
            avatar_url=avatar_url.strip() if avatar_url else None,
            bio=bio.strip() if bio else None,
            email=email if email else None,
            email_verified=email_verified == "true",
            phone=phone if phone else None,
            date_of_birth=date_of_birth or None,
            gender=gender or None,
            preferred_contact_method=preferred_contact_method or None,
            locale=locale.strip() if locale else None,
            timezone=timezone.strip() if timezone else None,
            address_line1=address_line1.strip() if address_line1 else None,
            address_line2=address_line2.strip() if address_line2 else None,
            city=city.strip() if city else None,
            region=region.strip() if region else None,
            postal_code=postal_code.strip() if postal_code else None,
            country_code=country_code.strip() if country_code else None,
            status=status or None,
            is_active=active,
            marketing_opt_in=marketing_opt_in == "true",
            notes=notes.strip() if notes else None,
            metadata_=_parse_json(metadata, "metadata") if metadata is not None else None,
        )
        people_service.update(db=db, person_id=customer_id, payload=data)
        after = people_service.get(db=db, person_id=customer_id)
        metadata_payload = build_changes_metadata(before, after)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="person",
            entity_id=str(customer_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata_payload,
        )
        # Update subscriber's account_start_date if provided
        if account_start_date:
            from app.schemas.subscriber import SubscriberUpdate
            from app.services.common import coerce_uuid
            subscriber = db.query(Subscriber).filter(
                Subscriber.person_id == coerce_uuid(customer_id)
            ).first()
            if subscriber:
                parsed_date = _parse_date(account_start_date)
                if parsed_date:
                    subscriber.account_start_date = parsed_date
                    db.commit()
        return RedirectResponse(
            url=f"/admin/customers/person/{customer_id}",
            status_code=303,
        )
    except HTTPException:
        raise
    except Exception as e:
        from app.web.admin import get_sidebar_stats, get_current_user
        sidebar_stats = get_sidebar_stats(db)
        current_user = get_current_user(request)
        try:
            customer = people_service.get(db=db, person_id=customer_id)
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
    legal_name: Optional[str] = Form(None),
    tax_id: Optional[str] = Form(None),
    domain: Optional[str] = Form(None),
    website: Optional[str] = Form(None),
    org_notes: Optional[str] = Form(None),
    org_account_start_date: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Update an organization."""
    try:
        from app.schemas.subscriber import OrganizationUpdate
        from app.services.common import coerce_uuid
        before = subscriber_service.organizations.get(db=db, organization_id=customer_id)
        data = OrganizationUpdate(
            name=name,
            legal_name=legal_name.strip() if legal_name else None,
            tax_id=tax_id.strip() if tax_id else None,
            domain=domain.strip() if domain else None,
            website=website.strip() if website else None,
            notes=org_notes.strip() if org_notes else None,
        )
        subscriber_service.organizations.update(db=db, organization_id=customer_id, payload=data)
        after = subscriber_service.organizations.get(db=db, organization_id=customer_id)
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
        # Update subscriber's account_start_date if provided
        if org_account_start_date:
            subscriber = (
                db.query(Subscriber)
                # TODO: Person model removed - Subscriber no longer has person_id FK
# .join(Person, Subscriber.person_id == Subscriber.id)
                .filter(Subscriber.organization_id == coerce_uuid(customer_id))
                .first()
            )
            if subscriber:
                parsed_date = _parse_date(org_account_start_date)
                if parsed_date:
                    subscriber.account_start_date = parsed_date
                    db.commit()
        return RedirectResponse(
            url=f"/admin/customers/organization/{customer_id}",
            status_code=303,
        )
    except Exception as e:
        from app.web.admin import get_sidebar_stats, get_current_user
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
    # TODO: PersonUpdate schema removed - use subscriber schemas
# from app.schemas.person import PersonUpdate

    person = people_service.get(db=db, person_id=customer_id)
    people_service.update(
        db=db,
        person_id=customer_id,
        payload=PersonUpdate(is_active=False, status="inactive"),
    )
    db.query(Subscriber).filter(Subscriber.person_id == person.id).update(
        {"is_active": False}
    )
    db.query(UserCredential).filter(UserCredential.person_id == person.id).update(
        {"is_active": False}
    )
    db.commit()
    after = people_service.get(db=db, person_id=customer_id)
    metadata_payload = build_changes_metadata(person, after)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="person",
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
    from uuid import UUID

    subscriber_service.organizations.get(db=db, organization_id=customer_id)
    org_uuid = UUID(customer_id)
    (
        db.query(Subscriber)
        # TODO: Person model removed - Subscriber no longer has person_id FK
# .join(Person, Subscriber.person_id == Subscriber.id)
        .filter(Subscriber.organization_id == org_uuid)
        .update({"is_active": False}, synchronize_session=False)
    )
    db.commit()
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
        person = people_service.get(db=db, person_id=customer_id)
        if person.is_active:
            raise HTTPException(status_code=409, detail="Deactivate customer before deleting.")
        if db.query(Subscriber).filter(Subscriber.person_id == person.id).count():
            raise HTTPException(status_code=409, detail="Delete subscriber before deleting customer.")
        db.query(UserCredential).filter(UserCredential.person_id == person.id).delete(synchronize_session=False)
        db.query(MFAMethod).filter(MFAMethod.person_id == person.id).delete(synchronize_session=False)
        db.query(Session).filter(Session.person_id == person.id).delete(synchronize_session=False)
        db.query(ApiKey).filter(ApiKey.person_id == person.id).delete(synchronize_session=False)
        db.commit()
        people_service.delete(db=db, person_id=customer_id)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="delete",
            entity_type="person",
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
        from app.web.admin import get_sidebar_stats, get_current_user
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
        subscriber_service.organizations.get(db=db, organization_id=customer_id)
        from uuid import UUID
        org_uuid = UUID(customer_id)
        if (
            db.query(Subscriber)
            # TODO: Person model removed - Subscriber no longer has person_id FK
# .join(Person, Subscriber.person_id == Subscriber.id)
            .filter(Subscriber.organization_id == org_uuid)
            .count()
        ):
            raise HTTPException(
                status_code=409,
                detail="Delete subscribers before deleting organization.",
            )
        subscriber_service.organizations.delete(db=db, organization_id=customer_id)
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
        from app.web.admin import get_sidebar_stats, get_current_user
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
    label: Optional[str] = Form(None),
    address_line1: str = Form(...),
    address_line2: Optional[str] = Form(None),
    city: Optional[str] = Form(None),
    region: Optional[str] = Form(None),
    postal_code: Optional[str] = Form(None),
    country_code: Optional[str] = Form(None),
    is_primary: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Create a new address for a subscriber."""
    from uuid import UUID
    from app.schemas.subscriber import AddressCreate
    from app.models.subscriber import AddressType

    try:
        # Map string to enum
        addr_type_map = {
            "service": AddressType.service,
            "billing": AddressType.billing,
            "mailing": AddressType.mailing,
        }
        addr_type = addr_type_map.get(address_type, AddressType.service)

        payload = AddressCreate(
            subscriber_id=UUID(subscriber_id),
            address_type=addr_type,
            label=label or None,
            address_line1=address_line1,
            address_line2=address_line2 or None,
            city=city or None,
            region=region or None,
            postal_code=postal_code or None,
            country_code=country_code or None,
            is_primary=is_primary == "true",
        )
        subscriber_service.addresses.create(db=db, payload=payload)

        # Redirect back to customer detail page
        redirect_url = f"/admin/customers/{customer_type}/{customer_id}"
        if request.headers.get("HX-Request"):
            return HTMLResponse(content="", headers={"HX-Redirect": redirect_url})
        return RedirectResponse(url=redirect_url, status_code=303)

    except Exception as e:
        from app.web.admin import get_sidebar_stats, get_current_user
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

    address = subscriber_service.addresses.update(
        db=db,
        address_id=address_id,
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


@router.delete("/addresses/{address_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("customer:write"))])
def delete_address(
    request: Request,
    address_id: str,
    db: Session = Depends(get_db),
):
    """Delete an address."""
    try:
        subscriber_service.addresses.delete(db=db, address_id=address_id)
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
    title: Optional[str] = Form(None),
    email: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    is_primary: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Create a new contact for an account."""
    from uuid import UUID

    try:
        row = {
            "first_name": first_name,
            "last_name": last_name,
            "title": title or None,
            "role": role,
            "email": email or "",
            "phone": phone or "",
            "is_primary": is_primary == "true",
        }
        organization_id = customer_id if customer_type == "organization" else None
        _create_account_roles_from_rows(
            db,
            str(UUID(account_id)),
            [row],
            organization_id=organization_id,
        )

        # Redirect back to customer detail page
        redirect_url = f"/admin/customers/{customer_type}/{customer_id}"
        if request.headers.get("HX-Request"):
            return HTMLResponse(content="", headers={"HX-Redirect": redirect_url})
        return RedirectResponse(url=redirect_url, status_code=303)

    except Exception as e:
        from app.web.admin import get_sidebar_stats, get_current_user
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
        subscriber_service.account_roles.delete(db=db, role_id=contact_id)
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
async def bulk_update_status(
    request: Request,
    db: Session = Depends(get_db),
):
    """Bulk update customer status (activate/deactivate)."""
    try:
        data = await request.json()
        customer_ids = data.get("customer_ids", [])
        new_status = data.get("status")

        if not customer_ids or not new_status:
            raise HTTPException(status_code=400, detail="customer_ids and status are required")

        if new_status not in ("active", "inactive"):
            raise HTTPException(status_code=400, detail="status must be 'active' or 'inactive'")

        is_active = new_status == "active"
        updated_count = 0
        errors = []

        for item in customer_ids:
            customer_id = item.get("id")
            customer_type = item.get("type")

            try:
                if customer_type == "person":
                    # TODO: PersonUpdate schema removed - use subscriber schemas
# from app.schemas.person import PersonUpdate
                    people_service.update(
                        db=db,
                        person_id=customer_id,
                        payload=PersonUpdate(is_active=is_active, status=new_status),
                    )
                    # Also update related subscribers
                    from uuid import UUID
                    person_uuid = UUID(customer_id)
                    db.query(Subscriber).filter(Subscriber.person_id == person_uuid).update(
                        {"is_active": is_active}
                    )
                    if not is_active:
                        # Deactivate credentials when deactivating person
                        person = people_service.get(db=db, person_id=customer_id)
                        db.query(UserCredential).filter(UserCredential.person_id == person.id).update(
                            {"is_active": False}
                        )
                elif customer_type == "organization":
                    # Organizations don't have is_active field directly, but their subscribers do
                    from uuid import UUID
                    org_uuid = UUID(customer_id)
                    (
                        db.query(Subscriber)
                        # TODO: Person model removed - Subscriber no longer has person_id FK
# .join(Person, Subscriber.person_id == Subscriber.id)
                        .filter(Subscriber.organization_id == org_uuid)
                        .update({"is_active": is_active}, synchronize_session=False)
                    )

                updated_count += 1
            except Exception as e:
                errors.append({"id": customer_id, "type": customer_type, "error": str(e)})

        db.commit()

        return {
            "success": True,
            "updated_count": updated_count,
            "errors": errors,
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/bulk/delete", dependencies=[Depends(require_permission("customer:delete"))])
async def bulk_delete_customers(
    request: Request,
    db: Session = Depends(get_db),
):
    """Bulk delete customers (only inactive customers without subscribers)."""
    try:
        data = await request.json()
        customer_ids = data.get("customer_ids", [])

        if not customer_ids:
            raise HTTPException(status_code=400, detail="customer_ids is required")

        deleted_count = 0
        skipped = []

        for item in customer_ids:
            customer_id = item.get("id")
            customer_type = item.get("type")

            try:
                if customer_type == "person":
                    person = people_service.get(db=db, person_id=customer_id)

                    # Check if person is active
                    if person.is_active:
                        skipped.append({"id": customer_id, "type": customer_type, "reason": "Customer is still active"})
                        continue

                    # Check if person has subscribers
                    if db.query(Subscriber).filter(Subscriber.person_id == person.id).count():
                        skipped.append({"id": customer_id, "type": customer_type, "reason": "Has associated subscribers"})
                        continue

                    # Delete related records
                    db.query(UserCredential).filter(UserCredential.person_id == person.id).delete(synchronize_session=False)
                    db.query(MFAMethod).filter(MFAMethod.person_id == person.id).delete(synchronize_session=False)
                    db.query(Session).filter(Session.person_id == person.id).delete(synchronize_session=False)
                    db.query(ApiKey).filter(ApiKey.person_id == person.id).delete(synchronize_session=False)
                    db.commit()

                    people_service.delete(db=db, person_id=customer_id)
                    deleted_count += 1

                elif customer_type == "organization":
                    from uuid import UUID
                    org_uuid = UUID(customer_id)

                    # Check if organization has subscribers
                    if (
                        db.query(Subscriber)
                        # TODO: Person model removed - Subscriber no longer has person_id FK
# .join(Person, Subscriber.person_id == Subscriber.id)
                        .filter(Subscriber.organization_id == org_uuid)
                        .count()
                    ):
                        skipped.append({"id": customer_id, "type": customer_type, "reason": "Has associated subscribers"})
                        continue

                    subscriber_service.organizations.delete(db=db, organization_id=customer_id)
                    deleted_count += 1

            except Exception as e:
                skipped.append({"id": customer_id, "type": customer_type, "reason": str(e)})

        return {
            "success": True,
            "deleted_count": deleted_count,
            "skipped": skipped,
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/export", dependencies=[Depends(require_permission("customer:read"))])
def export_customers(
    request: Request,
    export: str = Query("csv"),
    ids: str = Query("all"),
    search: Optional[str] = None,
    customer_type: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Export customers to CSV or Excel format."""
    import csv
    import io
    from datetime import datetime

    # Get customers based on selection
    customers = []

    if ids == "all":
        # Export all customers (with filters applied)
        if customer_type != "organization":
            people_query = (
                db.query(Subscriber)
                .join(Subscriber, Subscriber.person_id == Subscriber.id)
            )
            if search:
                people_query = people_query.filter(Subscriber.email.ilike(f"%{search}%"))
            people = people_query.order_by(Subscriber.created_at.desc()).all()
            for p in people:
                customers.append({
                    "id": str(p.id),
                    "type": "person",
                    "name": f"{p.first_name} {p.last_name}",
                    "email": p.email,
                    "phone": p.phone,
                    "is_active": "Active" if p.is_active else "Inactive",
                    "created_at": p.created_at.strftime('%Y-%m-%d %H:%M:%S') if p.created_at else "",
                })

        if customer_type != "person":
            orgs = subscriber_service.organizations.list(
                db=db,
                name=search if search else None,
                order_by="name",
                order_dir="asc",
                limit=10000,
                offset=0,
            )
            for o in orgs:
                customers.append({
                    "id": str(o.id),
                    "type": "organization",
                    "name": o.name,
                    "email": getattr(o, "email", ""),
                    "phone": getattr(o, "phone", ""),
                    "is_active": "Active" if getattr(o, "is_active", True) else "Inactive",
                    "created_at": o.created_at.strftime('%Y-%m-%d %H:%M:%S') if o.created_at else "",
                })
    else:
        # Export specific customers
        for item in ids.split(","):
            if ":" in item:
                ctype, cid = item.split(":", 1)
                try:
                    if ctype == "person":
                        p = people_service.get(db=db, person_id=cid)
                        customers.append({
                            "id": str(p.id),
                            "type": "person",
                            "name": f"{p.first_name} {p.last_name}",
                            "email": p.email,
                            "phone": p.phone,
                            "is_active": "Active" if p.is_active else "Inactive",
                            "created_at": p.created_at.strftime('%Y-%m-%d %H:%M:%S') if p.created_at else "",
                        })
                    elif ctype == "organization":
                        o = subscriber_service.organizations.get(db=db, organization_id=cid)
                        customers.append({
                            "id": str(o.id),
                            "type": "organization",
                            "name": o.name,
                            "email": getattr(o, "email", ""),
                            "phone": getattr(o, "phone", ""),
                            "is_active": "Active" if getattr(o, "is_active", True) else "Inactive",
                            "created_at": o.created_at.strftime('%Y-%m-%d %H:%M:%S') if o.created_at else "",
                        })
                except Exception:
                    continue

    # Generate CSV
    output = io.StringIO()
    fieldnames = ["id", "type", "name", "email", "phone", "is_active", "created_at"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for customer in customers:
        writer.writerow(customer)

    content = output.getvalue()
    output.close()

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"customers_export_{timestamp}.csv"

    return Response(
        content=content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
        },
    )
