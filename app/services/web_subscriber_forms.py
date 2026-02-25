"""Service helpers for subscriber create/edit web forms."""

import re
from typing import cast
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.auth import AuthProvider
from app.models.subscriber import Organization
from app.models.subscriber import Subscriber
from app.schemas.auth import UserCredentialCreate
from app.schemas.subscriber import SubscriberUpdate
from app.services import auth as auth_service
from app.services import subscriber as subscriber_service
from app.services.auth_flow import hash_password


def parse_customer_ref(value: str | None) -> tuple[str, UUID]:
    if not value:
        raise ValueError("customer_ref is required")
    if ":" not in value:
        raise ValueError("customer_ref must be selected from the list")
    ref_type, ref_id = value.split(":", 1)
    if ref_type not in ("person", "organization"):
        raise ValueError("customer_ref must be person or organization")
    return ref_type, UUID(ref_id)


def resolve_customer_ref(
    db: Session,
    customer_ref: str | None,
    customer_search: str | None,
) -> tuple[str, UUID] | None:
    if customer_ref:
        try:
            return parse_customer_ref(customer_ref)
        except ValueError:
            # Some clients can submit raw UUIDs from typeahead items.
            # Accept those by resolving against person/subscriber first, then organization.
            raw_ref = customer_ref.strip()
            try:
                raw_uuid = UUID(raw_ref)
            except (TypeError, ValueError):
                raw_uuid = None
            if raw_uuid:
                if db.get(Subscriber, raw_uuid):
                    return "person", raw_uuid
                if db.get(Organization, raw_uuid):
                    return "organization", raw_uuid
    search_term = (customer_search or "").strip()
    if not search_term:
        return None
    from app.services import customer_search as customer_search_service

    candidate_terms: list[str] = [search_term]

    # Support labels like: "John Doe (john@example.com)" by searching the inner token too.
    paren_match = re.search(r"\(([^()]+)\)\s*$", search_term)
    if paren_match:
        inner = paren_match.group(1).strip()
        outer = search_term[:paren_match.start()].strip()
        if inner:
            candidate_terms.append(inner)
        if outer:
            candidate_terms.append(outer)

    # Add conservative token fallback for full-name labels.
    parts = [part for part in re.split(r"\s+", search_term) if part]
    if len(parts) >= 2:
        candidate_terms.append(f"{parts[0]} {parts[-1]}")
    candidate_terms.extend(parts[:2])

    seen: set[str] = set()
    for term in candidate_terms:
        normalized = term.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        matches = customer_search_service.search(db=db, query=term, limit=2)
        if len(matches) == 1:
            return parse_customer_ref(matches[0].get("ref"))

    # If every strategy failed, keep message actionable for user flow.
    raise ValueError("No customer matches that search")


def resolve_subscriber_for_org(db: Session, org_id: UUID) -> UUID | None:
    return cast(
        UUID | None,
        db.query(Subscriber.id)
        .filter(Subscriber.organization_id == org_id)
        .order_by(Subscriber.created_at.asc())
        .scalar(),
    )


def resolve_form_customer_ids(
    db: Session,
    customer_ref: str | None,
    customer_search: str | None,
    subscriber_type: str | None,
    person_id: str | None,
    organization_id: str | None,
) -> tuple[str, UUID | None, UUID | None]:
    resolved_ref = resolve_customer_ref(db, customer_ref, customer_search)
    if resolved_ref:
        resolved_type, ref_id = resolved_ref
        person_uuid = ref_id if resolved_type == "person" else None
        org_uuid = ref_id if resolved_type == "organization" else None
        if resolved_type == "organization" and org_uuid and not person_uuid:
            person_uuid = resolve_subscriber_for_org(db, org_uuid)
        if not person_uuid:
            raise ValueError("person_id is required")
        return resolved_type, person_uuid, org_uuid

    if subscriber_type not in ("person", "organization"):
        raise ValueError("customer_ref is required")

    person_uuid = UUID(person_id) if person_id else None
    org_uuid = UUID(organization_id) if organization_id else None
    if subscriber_type == "person" and not person_uuid:
        raise ValueError("person_id is required for person subscribers")
    if subscriber_type == "organization" and not org_uuid:
        raise ValueError("organization_id is required for organization subscribers")
    if subscriber_type == "organization" and org_uuid and not person_uuid:
        person_uuid = resolve_subscriber_for_org(db, org_uuid)
    if not person_uuid:
        raise ValueError("person_id is required")
    return subscriber_type, person_uuid, org_uuid


def create_subscriber_with_optional_login(
    db: Session,
    subscriber_type: str,
    person_uuid: UUID,
    organization_uuid: UUID | None,
    subscriber_number: str | None,
    subscriber_category: str | None,
    notes: str | None,
    is_active: str | None,
    create_user: str | None,
    user_username: str | None,
    user_password: str | None,
):
    subscriber = db.get(Subscriber, person_uuid)
    if not subscriber:
        raise ValueError("Subscriber not found")

    if create_user == "true":
        if subscriber_type != "person":
            raise ValueError(
                "Customer portal logins can only be created for person subscribers."
            )
        if not user_username or not user_password:
            raise ValueError("Username and password are required to create a login.")
        existing = auth_service.user_credentials.list(
            db=db,
            subscriber_id=str(person_uuid),
            provider=AuthProvider.local.value,
            is_active=True,
            order_by="created_at",
            order_dir="desc",
            limit=1,
            offset=0,
        )
        if existing:
            raise ValueError("Customer already has a portal login.")

    payload = SubscriberUpdate(
        organization_id=organization_uuid if subscriber_type == "organization" else None,
        subscriber_number=subscriber_number.strip() if subscriber_number else None,
        category=subscriber_category.strip().lower() if subscriber_category else None,
        notes=notes.strip() if notes else None,
        is_active=is_active == "true",
    )
    subscriber = subscriber_service.subscribers.update(
        db=db,
        subscriber_id=str(person_uuid),
        payload=payload,
    )

    if create_user == "true":
        if user_username is None or user_password is None:
            raise ValueError("Username and password are required to create a login.")
        credential = UserCredentialCreate(
            subscriber_id=person_uuid,
            provider=AuthProvider.local,
            username=user_username.strip(),
            password_hash=hash_password(user_password),
        )
        auth_service.user_credentials.create(db=db, payload=credential)
    return subscriber


def load_subscriber_form_options(db: Session, limit: int = 500):
    people = subscriber_service.subscribers.list(
        db=db,
        organization_id=None,
        subscriber_type="person",
        order_by="created_at",
        order_dir="asc",
        limit=limit,
        offset=0,
    )
    organizations = subscriber_service.organizations.list(
        db=db,
        name=None,
        order_by="name",
        order_dir="asc",
        limit=limit,
        offset=0,
    )
    return people, organizations


def resolve_new_form_prefill(
    db: Session,
    *,
    subscriber_id: str | None,
    organization_id: str | None,
) -> tuple[str, str]:
    """Resolve customer prefill tokens for subscriber new form."""
    prefill_ref = ""
    prefill_label = ""
    if subscriber_id:
        try:
            subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
            prefill_ref = f"person:{subscriber.id}"
            prefill_label = f"{subscriber.first_name} {subscriber.last_name}".strip()
            if subscriber.email:
                prefill_label = f"{prefill_label} ({subscriber.email})"
            return prefill_ref, prefill_label
        except Exception:
            return "", ""
    if organization_id:
        try:
            organization = subscriber_service.organizations.get(
                db=db,
                organization_id=organization_id,
            )
            prefill_ref = f"organization:{organization.id}"
            prefill_label = organization.name
            if organization.domain:
                prefill_label = f"{prefill_label} ({organization.domain})"
            return prefill_ref, prefill_label
        except Exception:
            return "", ""
    return "", ""


def build_subscriber_update_form_values(
    *,
    customer_ref: str | None,
    customer_search: str | None,
    subscriber_type: str | None,
    person_id: str | None,
    organization_id: str | None,
    subscriber_number: str | None,
    subscriber_category: str | None,
    notes: str | None,
    is_active: str | None,
) -> dict:
    """Build field values for subscriber edit form error re-render."""
    return {
        "customer_ref": customer_ref or "",
        "customer_search": customer_search or "",
        "subscriber_type": subscriber_type or "",
        "person_id": person_id or "",
        "organization_id": organization_id or "",
        "subscriber_number": subscriber_number or "",
        "subscriber_category": subscriber_category or "",
        "notes": notes or "",
        "is_active": is_active == "true",
    }
