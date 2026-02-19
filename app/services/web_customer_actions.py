"""Service helpers for web/admin customer action routes."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.auth import ApiKey, MFAMethod, UserCredential
from app.models.auth import Session as AuthSession
from app.models.catalog import Subscription
from app.models.subscriber import (
    AddressType,
    ChannelType,
    Organization,
    Subscriber,
    SubscriberChannel,
    SubscriberStatus,
)
from app.schemas.audit import AuditEventCreate
from app.schemas.subscriber import (
    AddressCreate,
    OrganizationCreate,
    OrganizationUpdate,
    SubscriberCreate,
    SubscriberUpdate,
)
from app.services import audit as audit_service
from app.services import catalog as catalog_service
from app.services import customer_portal
from app.services import subscriber as subscriber_service
from app.services.common import coerce_uuid


def _parse_date(value: str | None) -> datetime | None:
    if not value or not value.strip():
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d")
        return parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _create_subscriber(db: Session, payload: dict[str, Any]) -> Subscriber:
    data = dict(payload)
    if not data.get("email"):
        data["email"] = f"customer-{uuid4().hex}@placeholder.local"
    if not data.get("first_name"):
        data["first_name"] = "Customer"
    if not data.get("last_name"):
        data["last_name"] = "User"
    return cast(
        Subscriber,
        subscriber_service.subscribers.create(db=db, payload=SubscriberCreate(**data)),
    )


def _create_subscriber_channels_from_rows(
    db: Session,
    account_id: str,
    contact_rows: list[dict],
) -> None:
    subscriber = db.get(Subscriber, account_id)
    if not subscriber:
        return
    for row in contact_rows:
        email = (row.get("email") or "").strip()
        phone = (row.get("phone") or "").strip()
        is_primary = bool(row.get("is_primary"))
        if email:
            exists = (
                db.query(SubscriberChannel)
                .filter(SubscriberChannel.subscriber_id == subscriber.id)
                .filter(SubscriberChannel.channel_type == ChannelType.email)
                .filter(SubscriberChannel.address == email)
                .first()
            )
            if not exists:
                db.add(
                    SubscriberChannel(
                        subscriber_id=subscriber.id,
                        channel_type=ChannelType.email,
                        address=email,
                        label=row.get("role") or row.get("title"),
                        is_primary=is_primary,
                    )
                )
        if phone:
            exists = (
                db.query(SubscriberChannel)
                .filter(SubscriberChannel.subscriber_id == subscriber.id)
                .filter(SubscriberChannel.channel_type == ChannelType.phone)
                .filter(SubscriberChannel.address == phone)
                .first()
            )
            if not exists:
                db.add(
                    SubscriberChannel(
                        subscriber_id=subscriber.id,
                        channel_type=ChannelType.phone,
                        address=phone,
                        label=row.get("role") or row.get("title"),
                        is_primary=is_primary,
                    )
                )
    db.flush()


def parse_contact_rows(contact_columns: dict[str, list[str]]) -> list[dict[str, Any]]:
    contact_first_name = contact_columns.get("first_name", [])
    contact_last_name = contact_columns.get("last_name", [])
    contact_title = contact_columns.get("title", [])
    contact_role = contact_columns.get("role", [])
    contact_email = contact_columns.get("email", [])
    contact_phone = contact_columns.get("phone", [])
    contact_is_primary = contact_columns.get("is_primary", [])

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
    rows: list[dict[str, Any]] = []
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


def build_error_contact_rows(contact_columns: dict[str, list[str]]) -> list[dict[str, Any]]:
    contact_first_name = contact_columns.get("first_name", [])
    contact_last_name = contact_columns.get("last_name", [])
    contact_title = contact_columns.get("title", [])
    contact_role = contact_columns.get("role", [])
    contact_email = contact_columns.get("email", [])
    contact_phone = contact_columns.get("phone", [])
    contact_is_primary = contact_columns.get("is_primary", [])
    rows: list[dict[str, Any]] = []
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
        rows.append(
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
    return rows


def create_customer_from_wizard(db: Session, data: dict[str, Any]) -> tuple[str, str]:
    customer_type = data.get("customer_type", "person")
    if customer_type == "person":
        existing_metadata = data.get("metadata")
        if isinstance(existing_metadata, dict):
            ingest_metadata = existing_metadata
        else:
            ingest_metadata = {}
        if not ingest_metadata.get("ingest"):
            ingest_metadata["ingest"] = {
                "source": "admin/customers/wizard",
                "received_at": datetime.now(UTC).isoformat(),
                "raw": dict(data),
                "cleaning_version": "v1",
            }
        email = (data.get("email") or "").strip()
        if not email:
            raise ValueError("email is required")
        existing = (
            db.query(Subscriber)
            .filter(func.lower(Subscriber.email) == email.lower())
            .first()
        )
        if existing:
            raise ValueError(f"A customer with email {email} already exists.")
        person = _create_subscriber(
            db=db,
            payload={
                "first_name": (data.get("first_name") or "").strip(),
                "last_name": (data.get("last_name") or "").strip(),
                "display_name": (data.get("display_name") or "").strip() or None,
                "email": email,
                "phone": (data.get("phone") or "").strip() or None,
                "date_of_birth": data.get("date_of_birth") or None,
                "gender": data.get("gender", "unknown"),
                "address_line1": (data.get("address_line1") or "").strip() or None,
                "address_line2": (data.get("address_line2") or "").strip() or None,
                "city": (data.get("city") or "").strip() or None,
                "region": (data.get("region") or "").strip() or None,
                "postal_code": (data.get("postal_code") or "").strip() or None,
                "country_code": (data.get("country_code") or "").strip() or None,
                "is_active": data.get("is_active", True),
                "status": data.get("status", "active"),
                "notes": (data.get("notes") or "").strip() or None,
                "metadata_": ingest_metadata,
            },
        )
        return "person", str(person.id)

    if customer_type == "organization":
        org_name = (data.get("name") or "").strip()
        if not org_name:
            raise ValueError("Organization name is required")
        existing_org = (
            db.query(Organization)
            .filter(func.lower(Organization.name) == org_name.lower())
            .first()
        )
        if existing_org:
            raise ValueError(f"An organization with name {org_name} already exists.")
        organization = subscriber_service.organizations.create(
            db,
            OrganizationCreate(
                name=org_name,
                legal_name=(data.get("legal_name") or "").strip() or None,
                tax_id=(data.get("tax_id") or "").strip() or None,
                domain=(data.get("domain") or "").strip() or None,
                website=(data.get("website") or "").strip() or None,
                address_line1=(data.get("address_line1") or "").strip() or None,
                address_line2=(data.get("address_line2") or "").strip() or None,
                city=(data.get("city") or "").strip() or None,
                region=(data.get("region") or "").strip() or None,
                postal_code=(data.get("postal_code") or "").strip() or None,
                country_code=(data.get("country_code") or "").strip() or None,
                notes=(data.get("notes") or "").strip() or None,
            ),
        )
        contacts = data.get("contacts", [])
        for contact_data in contacts:
            first_name = (contact_data.get("first_name") or "").strip()
            last_name = (contact_data.get("last_name") or "").strip()
            if not first_name or not last_name:
                continue
            _create_subscriber(
                db=db,
                payload={
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": (contact_data.get("email") or "").strip() or None,
                    "phone": (contact_data.get("phone") or "").strip() or None,
                    "organization_id": organization.id,
                    "is_active": True,
                    "status": "active",
                },
            )
        db.commit()
        return "organization", str(organization.id)

    raise ValueError("Invalid customer type")


def create_customer_from_form(
    db: Session,
    *,
    customer_type: str,
    form_data: dict[str, Any],
    contact_columns: dict[str, list[str]],
) -> tuple[str, str]:
    contact_rows = parse_contact_rows(contact_columns)
    if customer_type not in {"person", "organization"}:
        raise ValueError("customer_type must be person or organization")

    if customer_type == "person":
        normalized_email = _normalize_optional(form_data.get("email"))
        if not normalized_email:
            raise ValueError("email is required")
        existing = (
            db.query(Subscriber)
            .filter(func.lower(Subscriber.email) == normalized_email.lower())
            .first()
        )
        if existing:
            raise ValueError(f"A customer with email {normalized_email} already exists.")
        customer = _create_subscriber(
            db=db,
            payload={
                "first_name": form_data.get("first_name"),
                "last_name": form_data.get("last_name"),
                "display_name": _normalize_optional(form_data.get("display_name")),
                "avatar_url": _normalize_optional(form_data.get("avatar_url")),
                "email": normalized_email,
                "email_verified": form_data.get("email_verified") == "true",
                "phone": _normalize_optional(form_data.get("phone")),
                "date_of_birth": form_data.get("date_of_birth") or None,
                "gender": form_data.get("gender") or "unknown",
                "preferred_contact_method": form_data.get("preferred_contact_method") or None,
                "locale": _normalize_optional(form_data.get("locale")),
                "timezone": _normalize_optional(form_data.get("timezone")),
                "address_line1": _normalize_optional(form_data.get("address_line1")),
                "address_line2": _normalize_optional(form_data.get("address_line2")),
                "city": _normalize_optional(form_data.get("city")),
                "region": _normalize_optional(form_data.get("region")),
                "postal_code": _normalize_optional(form_data.get("postal_code")),
                "country_code": _normalize_optional(form_data.get("country_code")),
                "status": form_data.get("status") or "active",
                "is_active": form_data.get("is_active") == "true",
                "marketing_opt_in": form_data.get("marketing_opt_in") == "true",
                "notes": _normalize_optional(form_data.get("notes")),
                "metadata_": form_data.get("metadata_json"),
            },
        )
        if contact_rows:
            _create_subscriber_channels_from_rows(db, str(customer.id), contact_rows)
        return "person", str(customer.id)

    organization = subscriber_service.organizations.create(
        db=db,
        payload=OrganizationCreate(
            name=cast(str, form_data.get("name") or "").strip(),
            legal_name=_normalize_optional(form_data.get("legal_name")),
            tax_id=_normalize_optional(form_data.get("tax_id")),
            domain=_normalize_optional(form_data.get("domain")),
            website=_normalize_optional(form_data.get("website")),
            notes=_normalize_optional(form_data.get("org_notes")),
        ),
    )
    if contact_rows:
        first_contact = contact_rows[0]
        primary_person = _create_subscriber(
            db=db,
            payload={
                "first_name": first_contact["first_name"],
                "last_name": first_contact["last_name"],
                "email": first_contact["email"] or f"org-{organization.id}@placeholder.local",
                "phone": first_contact["phone"] or None,
                "organization_id": organization.id,
                "is_active": True,
                "account_start_date": _parse_date(form_data.get("org_account_start_date")),
            },
        )
        _create_subscriber_channels_from_rows(db, str(primary_person.id), contact_rows)
    return "organization", str(organization.id)


def create_impersonation_session(
    db: Session,
    request: Request,
    customer_type: str,
    customer_id: str,
    account_id: str,
    subscription_id: str | None,
    auth: dict,
) -> str:
    subscribers = []
    if customer_type == "person":
        subscriber = db.get(Subscriber, customer_id)
        subscribers = [subscriber] if subscriber else []
    else:
        org_uuid = UUID(customer_id)
        subscribers = (
            db.query(Subscriber)
            .filter(Subscriber.organization_id == org_uuid)
            .order_by(Subscriber.created_at.desc())
            .limit(50)
            .all()
        )

    accounts = [sub for sub in subscribers if sub]
    account_lookup = {str(acc.id): acc for acc in accounts}
    selected_account = account_lookup.get(account_id)
    if not selected_account:
        raise HTTPException(status_code=404, detail="Subscriber account not found")

    selected_subscription_id = None
    if subscription_id:
        subscription = catalog_service.subscriptions.get(db=db, subscription_id=subscription_id)
        if str(getattr(subscription, "subscriber_id", "")) != str(selected_account.id):
            raise HTTPException(status_code=404, detail="Subscription not found")
        selected_subscription_id = subscription.id
    else:
        active_subs = catalog_service.subscriptions.list(
            db=db,
            subscriber_id=str(selected_account.id),
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
                subscriber_id=str(selected_account.id),
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
        subscriber_id=selected_account.id,
        subscription_id=selected_subscription_id,
        return_to=f"/admin/customers/{customer_type}/{customer_id}",
    )

    actor_id_value = None
    if isinstance(auth, dict):
        actor_id_value = str(auth.get("subscriber_id") or auth.get("person_id") or "") or None

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
        metadata_={
            "customer_type": customer_type,
            "customer_id": customer_id,
            "subscription_id": str(selected_subscription_id) if selected_subscription_id else None,
        },
    )
    audit_service.audit_events.create(db=db, payload=audit_payload)
    return session_token


def update_person_customer(
    db: Session,
    customer_id: str,
    *,
    first_name: str,
    last_name: str,
    display_name: str | None,
    avatar_url: str | None,
    email: str | None,
    email_verified: str | None,
    phone: str | None,
    date_of_birth: str | None,
    gender: str | None,
    preferred_contact_method: str | None,
    locale: str | None,
    timezone_value: str | None,
    address_line1: str | None,
    address_line2: str | None,
    city: str | None,
    region: str | None,
    postal_code: str | None,
    country_code: str | None,
    status: str | None,
    is_active: str | None,
    marketing_opt_in: str | None,
    notes: str | None,
    account_start_date: str | None,
    metadata_json: dict | None,
):
    before = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    active = is_active == "true"
    data = {
        "first_name": first_name,
        "last_name": last_name,
        "display_name": _normalize_optional(display_name),
        "avatar_url": _normalize_optional(avatar_url),
        "email": email or None,
        "email_verified": email_verified == "true",
        "phone": phone or None,
        "date_of_birth": date_of_birth or None,
        "gender": gender or None,
        "preferred_contact_method": preferred_contact_method or None,
        "locale": _normalize_optional(locale),
        "timezone": _normalize_optional(timezone_value),
        "address_line1": _normalize_optional(address_line1),
        "address_line2": _normalize_optional(address_line2),
        "city": _normalize_optional(city),
        "region": _normalize_optional(region),
        "postal_code": _normalize_optional(postal_code),
        "country_code": _normalize_optional(country_code),
        "status": status or None,
        "is_active": active,
        "marketing_opt_in": marketing_opt_in == "true",
        "notes": _normalize_optional(notes),
        "metadata_": metadata_json,
    }
    subscriber_service.subscribers.update(
        db=db,
        subscriber_id=customer_id,
        payload=SubscriberUpdate.model_validate(data),
    )
    after = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    if account_start_date:
        subscriber = db.get(Subscriber, customer_id)
        if subscriber:
            parsed_date = _parse_date(account_start_date)
            if parsed_date:
                subscriber.account_start_date = parsed_date
                db.commit()
    return before, after


def update_organization_customer(
    db: Session,
    customer_id: str,
    *,
    name: str,
    legal_name: str | None,
    tax_id: str | None,
    domain: str | None,
    website: str | None,
    org_notes: str | None,
    org_account_start_date: str | None,
):
    before = subscriber_service.organizations.get(db=db, organization_id=customer_id)
    payload = OrganizationUpdate(
        name=name,
        legal_name=_normalize_optional(legal_name),
        tax_id=_normalize_optional(tax_id),
        domain=_normalize_optional(domain),
        website=_normalize_optional(website),
        notes=_normalize_optional(org_notes),
    )
    subscriber_service.organizations.update(
        db=db,
        organization_id=customer_id,
        payload=payload,
    )
    after = subscriber_service.organizations.get(db=db, organization_id=customer_id)
    if org_account_start_date:
        subscriber = (
            db.query(Subscriber)
            .filter(Subscriber.organization_id == coerce_uuid(customer_id))
            .first()
        )
        if subscriber:
            parsed_date = _parse_date(org_account_start_date)
            if parsed_date:
                subscriber.account_start_date = parsed_date
                db.commit()
    return before, after


def deactivate_person_customer(db: Session, customer_id: str):
    before = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    subscriber_service.subscribers.update(
        db=db,
        subscriber_id=customer_id,
        payload=SubscriberUpdate(is_active=False, status=SubscriberStatus.suspended),
    )
    db.query(UserCredential).filter(UserCredential.subscriber_id == before.id).update(
        {"is_active": False}
    )
    db.commit()
    after = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    return before, after


def deactivate_organization_customer(db: Session, customer_id: str) -> None:
    subscriber_service.organizations.get(db=db, organization_id=customer_id)
    org_uuid = UUID(customer_id)
    (
        db.query(Subscriber)
        .filter(Subscriber.organization_id == org_uuid)
        .update({"is_active": False}, synchronize_session=False)
    )
    db.commit()


def delete_person_customer(db: Session, customer_id: str) -> None:
    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    if subscriber.is_active:
        raise HTTPException(status_code=409, detail="Deactivate customer before deleting.")

    db.query(UserCredential).filter(UserCredential.subscriber_id == subscriber.id).delete(
        synchronize_session=False
    )
    db.query(MFAMethod).filter(MFAMethod.subscriber_id == subscriber.id).delete(
        synchronize_session=False
    )
    db.query(AuthSession).filter(AuthSession.subscriber_id == subscriber.id).delete(
        synchronize_session=False
    )
    db.query(ApiKey).filter(ApiKey.subscriber_id == subscriber.id).delete(
        synchronize_session=False
    )
    db.commit()
    subscriber_service.subscribers.delete(db=db, subscriber_id=customer_id)


def delete_organization_customer(db: Session, customer_id: str) -> None:
    subscriber_service.organizations.get(db=db, organization_id=customer_id)
    org_uuid = UUID(customer_id)
    if db.query(Subscriber).filter(Subscriber.organization_id == org_uuid).count():
        raise HTTPException(
            status_code=409,
            detail="Delete subscribers before deleting organization.",
        )
    subscriber_service.organizations.delete(db=db, organization_id=customer_id)


def bulk_update_customer_status(
    db: Session,
    customer_ids: list[dict[str, str]],
    is_active: bool,
) -> dict[str, Any]:
    from app.models.subscriber import SubscriberStatus

    updated_count = 0
    errors: list[dict[str, str]] = []
    for item in customer_ids:
        customer_id = item.get("id")
        customer_type = item.get("type")
        try:
            if customer_type in {"person", "subscriber"}:
                subscriber = db.get(Subscriber, customer_id)
                if not subscriber:
                    errors.append({"id": str(customer_id), "type": str(customer_type), "error": "Subscriber not found"})
                    continue
                subscriber.is_active = is_active
                subscriber.status = (
                    SubscriberStatus.active if is_active else SubscriberStatus.suspended
                )
                if not is_active:
                    db.query(UserCredential).filter(
                        UserCredential.subscriber_id == subscriber.id
                    ).update({"is_active": False}, synchronize_session=False)
            elif customer_type == "organization":
                org_uuid = UUID(str(customer_id))
                (
                    db.query(Subscriber)
                    .filter(Subscriber.organization_id == org_uuid)
                    .update({"is_active": is_active}, synchronize_session=False)
                )
            updated_count += 1
        except Exception as exc:
            errors.append({"id": str(customer_id), "type": str(customer_type), "error": str(exc)})
    db.commit()
    return {
        "success": True,
        "updated_count": updated_count,
        "errors": errors,
    }


def bulk_delete_customers(
    db: Session,
    customer_ids: list[dict[str, str]],
) -> dict[str, Any]:
    deleted_count = 0
    skipped: list[dict[str, str]] = []
    for item in customer_ids:
        customer_id = item.get("id")
        customer_type = item.get("type")
        try:
            if customer_type in {"person", "subscriber"}:
                subscriber = db.get(Subscriber, customer_id)
                if not subscriber:
                    skipped.append({"id": str(customer_id), "type": str(customer_type), "reason": "Subscriber not found"})
                    continue
                if subscriber.is_active:
                    skipped.append({"id": str(customer_id), "type": str(customer_type), "reason": "Customer is still active"})
                    continue
                if db.query(Subscription).filter(Subscription.subscriber_id == subscriber.id).count():
                    skipped.append({"id": str(customer_id), "type": str(customer_type), "reason": "Has associated subscriptions"})
                    continue
                db.query(UserCredential).filter(UserCredential.subscriber_id == subscriber.id).delete(
                    synchronize_session=False
                )
                db.query(MFAMethod).filter(MFAMethod.subscriber_id == subscriber.id).delete(
                    synchronize_session=False
                )
                db.query(AuthSession).filter(AuthSession.subscriber_id == subscriber.id).delete(
                    synchronize_session=False
                )
                db.query(ApiKey).filter(ApiKey.subscriber_id == subscriber.id).delete(
                    synchronize_session=False
                )
                subscriber_service.subscribers.delete(db=db, subscriber_id=str(customer_id))
                deleted_count += 1
            elif customer_type == "organization":
                org_uuid = UUID(str(customer_id))
                if db.query(Subscriber).filter(Subscriber.organization_id == org_uuid).count():
                    skipped.append({"id": str(customer_id), "type": str(customer_type), "reason": "Has associated subscribers"})
                    continue
                subscriber_service.organizations.delete(db=db, organization_id=str(customer_id))
                deleted_count += 1
        except Exception as exc:
            skipped.append({"id": str(customer_id), "type": str(customer_type), "reason": str(exc)})
    return {
        "success": True,
        "deleted_count": deleted_count,
        "skipped": skipped,
    }


def export_customers_csv(
    db: Session,
    *,
    ids: str,
    search: str | None,
    customer_type: str | None,
) -> tuple[str, str]:
    customers: list[dict[str, str]] = []
    if ids == "all":
        if customer_type != "organization":
            people_query = db.query(Subscriber).filter(Subscriber.organization_id.is_(None))
            if search:
                people_query = people_query.filter(Subscriber.email.ilike(f"%{search}%"))
            people = people_query.order_by(Subscriber.created_at.desc()).all()
            for person in people:
                customers.append(
                    {
                        "id": str(person.id),
                        "type": "person",
                        "name": f"{person.first_name} {person.last_name}",
                        "email": person.email,
                        "phone": person.phone or "",
                        "is_active": "Active" if person.is_active else "Inactive",
                        "created_at": person.created_at.strftime("%Y-%m-%d %H:%M:%S") if person.created_at else "",
                    }
                )
        if customer_type != "person":
            orgs_query = db.query(Organization)
            if search:
                orgs_query = orgs_query.filter(Organization.name.ilike(f"%{search}%"))
            orgs = orgs_query.order_by(Organization.name.asc()).all()
            for org in orgs:
                customers.append(
                    {
                        "id": str(org.id),
                        "type": "organization",
                        "name": org.name,
                        "email": getattr(org, "email", ""),
                        "phone": getattr(org, "phone", ""),
                        "is_active": "Active" if getattr(org, "is_active", True) else "Inactive",
                        "created_at": org.created_at.strftime("%Y-%m-%d %H:%M:%S") if org.created_at else "",
                    }
                )
    else:
        for item in ids.split(","):
            if ":" not in item:
                continue
            ctype, cid = item.split(":", 1)
            try:
                if ctype == "person":
                    person = subscriber_service.subscribers.get(db=db, subscriber_id=cid)
                    customers.append(
                        {
                            "id": str(person.id),
                            "type": "person",
                            "name": f"{person.first_name} {person.last_name}",
                            "email": person.email,
                            "phone": person.phone or "",
                            "is_active": "Active" if person.is_active else "Inactive",
                            "created_at": person.created_at.strftime("%Y-%m-%d %H:%M:%S") if person.created_at else "",
                        }
                    )
                elif ctype == "organization":
                    org = subscriber_service.organizations.get(db=db, organization_id=cid)
                    customers.append(
                        {
                            "id": str(org.id),
                            "type": "organization",
                            "name": org.name,
                            "email": getattr(org, "email", ""),
                            "phone": getattr(org, "phone", ""),
                            "is_active": "Active" if getattr(org, "is_active", True) else "Inactive",
                            "created_at": org.created_at.strftime("%Y-%m-%d %H:%M:%S") if org.created_at else "",
                        }
                    )
            except Exception:
                continue
    output = io.StringIO()
    fieldnames = ["id", "type", "name", "email", "phone", "is_active", "created_at"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for customer in customers:
        writer.writerow(customer)
    content = output.getvalue()
    output.close()
    filename = f"customers_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return content, filename


def _status_from_legacy(value: str | None, is_active: bool | None = None) -> SubscriberStatus | None:
    if is_active is not None and not is_active:
        return SubscriberStatus.suspended
    if not value:
        return None
    normalized = str(value).strip().lower()
    mapping = {
        "active": SubscriberStatus.active,
        "customer": SubscriberStatus.active,
        "subscriber": SubscriberStatus.active,
        "lead": SubscriberStatus.active,
        "contact": SubscriberStatus.active,
        "inactive": SubscriberStatus.suspended,
        "suspended": SubscriberStatus.suspended,
        "delinquent": SubscriberStatus.delinquent,
        "canceled": SubscriberStatus.canceled,
    }
    return mapping.get(normalized)


def convert_contact_to_subscriber(
    db: Session,
    *,
    person_id: UUID,
    account_status: str | None,
) -> tuple[Subscriber, bool]:
    person = db.get(Subscriber, person_id)
    if not person:
        raise HTTPException(status_code=404, detail="Subscriber not found")
    person.is_active = True
    person.status = _status_from_legacy(account_status, is_active=True) or SubscriberStatus.active
    db.commit()
    db.refresh(person)
    return person, not bool(person.email)


def create_customer_address(
    db: Session,
    *,
    subscriber_id: str,
    address_type: str,
    label: str | None,
    address_line1: str,
    address_line2: str | None,
    city: str | None,
    region: str | None,
    postal_code: str | None,
    country_code: str | None,
    is_primary: str | None,
) -> None:
    addr_type_map = {
        "service": AddressType.service,
        "billing": AddressType.billing,
        "mailing": AddressType.mailing,
    }
    payload = AddressCreate(
        subscriber_id=UUID(subscriber_id),
        address_type=addr_type_map.get(address_type, AddressType.service),
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


def delete_customer_address(db: Session, *, address_id: str) -> None:
    subscriber_service.addresses.delete(db=db, address_id=address_id)


def create_customer_contact(
    db: Session,
    *,
    account_id: str,
    first_name: str,
    last_name: str,
    role: str,
    title: str | None,
    email: str | None,
    phone: str | None,
    is_primary: str | None,
) -> None:
    row = {
        "first_name": first_name,
        "last_name": last_name,
        "title": title or None,
        "role": role,
        "email": email or "",
        "phone": phone or "",
        "is_primary": is_primary == "true",
    }
    _create_subscriber_channels_from_rows(db, str(UUID(account_id)), [row])


def delete_customer_contact(db: Session, *, contact_id: str) -> None:
    channel = db.get(SubscriberChannel, contact_id)
    if channel:
        db.delete(channel)
        db.commit()
