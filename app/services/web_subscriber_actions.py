"""Service helpers for web/admin subscriber action routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.schemas.subscriber import SubscriberUpdate
from app.services import subscriber as subscriber_service
from app.services.web_subscriber_forms import (
    create_subscriber_with_optional_login,
    resolve_form_customer_ids,
)


def deactivate_subscriber(db: Session, subscriber_id: UUID):
    before = subscriber_service.subscribers.get(db=db, subscriber_id=str(subscriber_id))
    subscriber_service.subscribers.update(
        db=db,
        subscriber_id=str(subscriber_id),
        payload=SubscriberUpdate(is_active=False),
    )
    after = subscriber_service.subscribers.get(db=db, subscriber_id=str(subscriber_id))
    return before, after


def create_subscriber_from_form(
    db: Session,
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
    create_user: str | None,
    user_username: str | None,
    user_password: str | None,
):
    resolved_type, person_uuid, org_uuid = resolve_form_customer_ids(
        db=db,
        customer_ref=customer_ref,
        customer_search=customer_search,
        subscriber_type=subscriber_type,
        person_id=person_id,
        organization_id=organization_id,
    )
    if not person_uuid:
        raise ValueError("person_id is required")
    return create_subscriber_with_optional_login(
        db=db,
        subscriber_type=resolved_type,
        person_uuid=person_uuid,
        organization_uuid=org_uuid,
        subscriber_number=subscriber_number,
        subscriber_category=subscriber_category,
        notes=notes,
        is_active=is_active,
        create_user=create_user,
        user_username=user_username,
        user_password=user_password,
    )


def build_subscriber_create_form_values(
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
    create_user: str | None,
    user_username: str | None,
) -> dict:
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
        "create_user": create_user == "true",
        "user_username": user_username or "",
    }


def update_subscriber_from_form(
    db: Session,
    *,
    subscriber_id: UUID,
    customer_ref: str | None,
    customer_search: str | None,
    subscriber_type: str | None,
    person_id: str | None,
    organization_id: str | None,
    subscriber_number: str | None,
    subscriber_category: str | None,
    notes: str | None,
    is_active: str | None,
):
    before = subscriber_service.subscribers.get(db=db, subscriber_id=str(subscriber_id))
    resolved_type, _, org_uuid = resolve_form_customer_ids(
        db=db,
        customer_ref=customer_ref,
        customer_search=customer_search,
        subscriber_type=subscriber_type,
        person_id=person_id,
        organization_id=organization_id,
    )
    active = is_active == "true"
    payload = SubscriberUpdate(
        organization_id=org_uuid if resolved_type == "organization" else None,
        subscriber_number=subscriber_number.strip() if subscriber_number else None,
        category=subscriber_category.strip().lower() if subscriber_category else None,
        notes=notes.strip() if notes else None,
        is_active=active,
    )
    subscriber = subscriber_service.subscribers.update(
        db=db,
        subscriber_id=str(subscriber_id),
        payload=payload,
    )
    after = subscriber_service.subscribers.get(db=db, subscriber_id=str(subscriber_id))
    return subscriber, before, after


def delete_subscriber(db: Session, subscriber_id: UUID) -> None:
    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=str(subscriber_id))
    if subscriber.is_active:
        raise HTTPException(status_code=409, detail="Deactivate subscriber before deleting.")
    subscriber_service.subscribers.delete(db=db, subscriber_id=str(subscriber_id))


def bulk_set_subscriber_status(db: Session, subscriber_ids: list[str], is_active: bool) -> int:
    updated_count = 0
    for subscriber_id in subscriber_ids:
        try:
            subscriber_service.subscribers.update(
                db=db,
                subscriber_id=str(subscriber_id),
                payload=SubscriberUpdate(is_active=is_active),
            )
            updated_count += 1
        except Exception:
            continue
    return updated_count


def bulk_delete_inactive_subscribers(db: Session, subscriber_ids: list[str]) -> tuple[int, int]:
    deleted_count = 0
    skipped_active = 0
    for subscriber_id in subscriber_ids:
        try:
            subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
            if subscriber.is_active:
                skipped_active += 1
                continue
            subscriber_service.subscribers.delete(db=db, subscriber_id=str(subscriber_id))
            deleted_count += 1
        except Exception:
            continue
    return deleted_count, skipped_active
