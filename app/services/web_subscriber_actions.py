"""Service helpers for web/admin subscriber action routes."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.models.subscriber import SubscriberCategory
from app.schemas.subscriber import SubscriberUpdate
from app.services import subscriber as subscriber_service
from app.services import web_customer_actions as web_customer_actions_service
from app.services import web_system_restore_tool as web_system_restore_tool_service
from app.services.web_subscriber_forms import (
    create_subscriber_with_optional_login,
    resolve_form_customer_ids,
    resolve_subscriber_for_org,
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


def _parse_category(value: str | None) -> SubscriberCategory | None:
    raw = (value or "").strip().lower()
    if not raw:
        return None
    try:
        return SubscriberCategory(raw)
    except ValueError:
        return None


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


def create_subscriber_from_full_form(
    db: Session,
    *,
    customer_type: str,
    form_data: dict[str, Any],
    contact_columns: dict[str, list[str]],
    subscriber_number: str | None,
    subscriber_category: str | None,
    subscriber_notes: str | None,
    is_active: str | None,
):
    created_type, created_id = web_customer_actions_service.create_customer_from_form(
        db=db,
        customer_type=customer_type,
        form_data=form_data,
        contact_columns=contact_columns,
    )

    if created_type == "person":
        subscriber_type = "person"
        person_uuid = UUID(created_id)
        organization_uuid = None
    else:
        subscriber_type = "organization"
        organization_uuid = UUID(created_id)
        resolved_person_uuid = resolve_subscriber_for_org(db, organization_uuid)
        if not resolved_person_uuid:
            raise ValueError("Could not resolve a primary subscriber for the organization.")
        person_uuid = resolved_person_uuid

    notes_value = subscriber_notes
    if not notes_value and customer_type == "person":
        notes_value = form_data.get("notes")

    return create_subscriber_with_optional_login(
        db=db,
        subscriber_type=subscriber_type,
        person_uuid=person_uuid,
        organization_uuid=organization_uuid,
        subscriber_number=subscriber_number,
        subscriber_category=subscriber_category,
        notes=notes_value,
        is_active=is_active,
        create_user=None,
        user_username=None,
        user_password=None,
    )


def update_subscriber_from_full_form(
    db: Session,
    *,
    subscriber_id: UUID,
    customer_type: str,
    form_data: dict[str, Any],
    subscriber_number: str | None,
    subscriber_category: str | None,
    subscriber_notes: str | None,
    is_active: str | None,
):
    before = subscriber_service.subscribers.get(db=db, subscriber_id=str(subscriber_id))
    if not before:
        raise ValueError("Subscriber not found")

    if customer_type == "person":
        web_customer_actions_service.update_person_customer(
            db=db,
            customer_id=str(subscriber_id),
            first_name=form_data.get("first_name") or "",
            last_name=form_data.get("last_name") or "",
            display_name=form_data.get("display_name"),
            avatar_url=form_data.get("avatar_url"),
            email=form_data.get("email"),
            email_verified=form_data.get("email_verified"),
            phone=form_data.get("phone"),
            date_of_birth=form_data.get("date_of_birth"),
            gender=form_data.get("gender"),
            preferred_contact_method=form_data.get("preferred_contact_method"),
            locale=form_data.get("locale"),
            timezone_value=form_data.get("timezone"),
            address_line1=form_data.get("address_line1"),
            address_line2=form_data.get("address_line2"),
            city=form_data.get("city"),
            region=form_data.get("region"),
            postal_code=form_data.get("postal_code"),
            country_code=form_data.get("country_code"),
            status=form_data.get("status"),
            is_active=is_active,
            marketing_opt_in=form_data.get("marketing_opt_in"),
            notes=form_data.get("notes"),
            account_start_date=form_data.get("account_start_date"),
            metadata_json=form_data.get("metadata_json"),
        )
    elif customer_type == "organization":
        if not before.organization_id:
            raise ValueError("Subscriber is not linked to an organization.")
        web_customer_actions_service.update_organization_customer(
            db=db,
            customer_id=str(before.organization_id),
            name=form_data.get("name") or "",
            legal_name=form_data.get("legal_name"),
            tax_id=form_data.get("tax_id"),
            domain=form_data.get("domain"),
            website=form_data.get("website"),
            org_notes=form_data.get("org_notes"),
            org_account_start_date=form_data.get("org_account_start_date"),
        )
    else:
        raise ValueError("customer_type must be person or organization")

    payload = SubscriberUpdate(
        subscriber_number=subscriber_number.strip() if subscriber_number else None,
        category=_parse_category(subscriber_category),
        notes=(subscriber_notes or form_data.get("notes") or "").strip() or None,
        is_active=is_active == "true",
    )
    updated = subscriber_service.subscribers.update(
        db=db,
        subscriber_id=str(subscriber_id),
        payload=payload,
    )
    after = subscriber_service.subscribers.get(db=db, subscriber_id=str(subscriber_id))
    return updated, before, after


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
        category=_parse_category(subscriber_category),
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


def delete_subscriber(db: Session, subscriber_id: UUID, actor_id: str | None = None) -> None:
    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=str(subscriber_id))
    if subscriber.is_active:
        raise HTTPException(status_code=409, detail="Deactivate subscriber before deleting.")
    web_system_restore_tool_service.mark_subscriber_deleted(
        db=db,
        subscriber_id=str(subscriber_id),
        actor_id=actor_id,
    )


def bulk_set_subscriber_status(db: Session, subscriber_ids: list[str], is_active: bool) -> tuple[int, int]:
    updated_count = 0
    failed_count = 0
    for subscriber_id in subscriber_ids:
        try:
            subscriber_service.subscribers.update(
                db=db,
                subscriber_id=str(subscriber_id),
                payload=SubscriberUpdate(is_active=is_active),
            )
            updated_count += 1
        except Exception as exc:
            logger.error("Failed to set status for subscriber %s: %s", subscriber_id, exc)
            failed_count += 1
            continue
    return updated_count, failed_count


def bulk_delete_inactive_subscribers(
    db: Session,
    subscriber_ids: list[str],
    actor_id: str | None = None,
) -> tuple[int, int, int]:
    deleted_count = 0
    skipped_active = 0
    failed_count = 0
    for subscriber_id in subscriber_ids:
        try:
            subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
            if subscriber.is_active:
                skipped_active += 1
                continue
            web_system_restore_tool_service.mark_subscriber_deleted(
                db=db,
                subscriber_id=str(subscriber_id),
                actor_id=actor_id,
            )
            deleted_count += 1
        except Exception as exc:
            logger.error("Failed to delete subscriber %s: %s", subscriber_id, exc)
            failed_count += 1
            continue
    return deleted_count, skipped_active, failed_count
