"""Customer portal contact management."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber, SubscriberContact
from app.services.customer_portal_context import resolve_customer_account

CONTACT_TYPES = ("general", "billing", "technical", "installation", "emergency")


@dataclass(frozen=True)
class ContactForm:
    full_name: str
    phone: str | None
    email: str | None
    relationship: str | None
    contact_type: str
    is_authorized: bool
    receives_notifications: bool
    is_billing_contact: bool
    notes: str | None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def normalize_contact_form(
    *,
    full_name: str,
    phone: str | None,
    email: str | None,
    relationship: str | None,
    contact_type: str | None,
    is_authorized: bool,
    receives_notifications: bool,
    is_billing_contact: bool,
    notes: str | None,
) -> ContactForm:
    normalized_type = (contact_type or "general").strip().lower()
    if normalized_type not in CONTACT_TYPES:
        normalized_type = "general"
    return ContactForm(
        full_name=(full_name or "").strip(),
        phone=_clean(phone),
        email=_clean(email.lower() if email else None),
        relationship=_clean(relationship),
        contact_type=normalized_type,
        is_authorized=bool(is_authorized),
        receives_notifications=bool(receives_notifications),
        is_billing_contact=bool(is_billing_contact or normalized_type == "billing"),
        notes=_clean(notes),
    )


def _require_subscriber_id(customer: dict, db: Session) -> str:
    subscriber_id, _subscription_id = resolve_customer_account(customer, db)
    subscriber_id = subscriber_id or customer.get("subscriber_id")
    if not subscriber_id:
        raise ValueError("Unable to resolve customer account.")
    return str(subscriber_id)


def _subscriber(db: Session, subscriber_id: str) -> Subscriber | None:
    try:
        return db.get(Subscriber, UUID(str(subscriber_id)))
    except ValueError:
        return None


def duplicate_warnings(
    db: Session,
    *,
    subscriber_id: str,
    email: str | None,
    phone: str | None,
    exclude_contact_id: str | None = None,
) -> list[str]:
    warnings: list[str] = []
    subscriber_uuid = UUID(str(subscriber_id))
    exclude_uuid = UUID(str(exclude_contact_id)) if exclude_contact_id else None

    if email:
        existing_subscriber = db.scalar(
            select(Subscriber).where(
                Subscriber.id != subscriber_uuid,
                func.lower(Subscriber.email) == email.lower(),
            )
        )
        if existing_subscriber:
            warnings.append(
                "This email is already used by another subscriber account."
            )
        contact_query = select(SubscriberContact).where(
            func.lower(SubscriberContact.email) == email.lower()
        )
        if exclude_uuid:
            contact_query = contact_query.where(SubscriberContact.id != exclude_uuid)
        if db.scalar(contact_query):
            warnings.append("This email is already used by another linked contact.")

    if phone:
        existing_phone_subscriber = db.scalar(
            select(Subscriber).where(
                Subscriber.id != subscriber_uuid,
                Subscriber.phone == phone,
            )
        )
        if existing_phone_subscriber:
            warnings.append(
                "This phone number is already used by another subscriber account."
            )
        phone_query = select(SubscriberContact).where(SubscriberContact.phone == phone)
        if exclude_uuid:
            phone_query = phone_query.where(SubscriberContact.id != exclude_uuid)
        if db.scalar(phone_query):
            warnings.append(
                "This phone number is already used by another linked contact."
            )

    return warnings


def get_contacts_page(db: Session, customer: dict) -> dict:
    subscriber_id = _require_subscriber_id(customer, db)
    subscriber = _subscriber(db, subscriber_id)
    contacts = db.scalars(
        select(SubscriberContact)
        .where(SubscriberContact.subscriber_id == UUID(str(subscriber_id)))
        .order_by(SubscriberContact.created_at.desc())
    ).all()
    return {
        "subscriber": subscriber,
        "contacts": contacts,
        "contact_types": CONTACT_TYPES,
    }


def create_contact(db: Session, customer: dict, form: ContactForm) -> list[str]:
    if not form.full_name:
        raise ValueError("Full name is required.")
    subscriber_id = _require_subscriber_id(customer, db)
    warnings = duplicate_warnings(
        db,
        subscriber_id=subscriber_id,
        email=form.email,
        phone=form.phone,
    )
    contact = SubscriberContact(
        subscriber_id=UUID(str(subscriber_id)),
        full_name=form.full_name,
        phone=form.phone,
        email=form.email,
        relationship=form.relationship,
        contact_type=form.contact_type,
        is_authorized=form.is_authorized,
        receives_notifications=form.receives_notifications,
        is_billing_contact=form.is_billing_contact,
        notes=form.notes,
    )
    db.add(contact)
    db.commit()
    return warnings


def update_contact(
    db: Session, customer: dict, contact_id: str, form: ContactForm
) -> list[str]:
    if not form.full_name:
        raise ValueError("Full name is required.")
    subscriber_id = _require_subscriber_id(customer, db)
    contact = db.scalar(
        select(SubscriberContact).where(
            SubscriberContact.id == UUID(str(contact_id)),
            SubscriberContact.subscriber_id == UUID(str(subscriber_id)),
        )
    )
    if not contact:
        raise ValueError("Contact not found.")
    warnings = duplicate_warnings(
        db,
        subscriber_id=subscriber_id,
        email=form.email,
        phone=form.phone,
        exclude_contact_id=contact_id,
    )
    contact.full_name = form.full_name
    contact.phone = form.phone
    contact.email = form.email
    contact.relationship = form.relationship
    contact.contact_type = form.contact_type
    contact.is_authorized = form.is_authorized
    contact.receives_notifications = form.receives_notifications
    contact.is_billing_contact = form.is_billing_contact
    contact.notes = form.notes
    db.commit()
    return warnings
