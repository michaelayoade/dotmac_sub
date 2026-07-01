"""CRM customer webhook service helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.subscriber import Subscriber, SubscriberCategory, SubscriberStatus
from app.schemas.audit import AuditEventCreate
from app.schemas.subscriber import SubscriberCreate
from app.services import audit as audit_service
from app.services import subscriber as subscriber_service


def _text(value: Any) -> str:
    return str(value or "").strip()


def _name_parts(payload: dict[str, Any]) -> tuple[str, str, str]:
    display_name = (
        _text(payload.get("display_name"))
        or _text(payload.get("name"))
        or _text(payload.get("full_name"))
        or _text(payload.get("customer_name"))
    )
    first_name = _text(payload.get("first_name"))
    last_name = _text(payload.get("last_name"))
    if not first_name and not last_name and display_name:
        parts = display_name.split()
        if len(parts) == 1:
            first_name, last_name = parts[0], "Customer"
        else:
            first_name, last_name = " ".join(parts[:-1]), parts[-1]
    if not display_name:
        display_name = " ".join(part for part in (first_name, last_name) if part)
    return first_name, last_name, display_name


def _address_fields(payload: dict[str, Any]) -> dict[str, Any]:
    address = payload.get("address")
    if isinstance(address, dict):
        return {
            "address_line1": _text(
                address.get("address_line1")
                or address.get("line1")
                or address.get("street")
                or address.get("address")
            )
            or None,
            "address_line2": _text(address.get("address_line2") or address.get("line2"))
            or None,
            "city": _text(address.get("city")) or None,
            "region": _text(address.get("region") or address.get("state")) or None,
            "postal_code": _text(address.get("postal_code") or address.get("postcode"))
            or None,
            "country_code": _text(address.get("country_code") or address.get("country"))
            or None,
        }
    return {
        "address_line1": _text(address or payload.get("address_line1")) or None,
        "address_line2": _text(payload.get("address_line2")) or None,
        "city": _text(payload.get("city")) or None,
        "region": _text(payload.get("region") or payload.get("state")) or None,
        "postal_code": _text(payload.get("postal_code") or payload.get("postcode"))
        or None,
        "country_code": _text(payload.get("country_code") or payload.get("country"))
        or None,
    }


def _status(value: Any) -> SubscriberStatus:
    raw = _text(value).lower() or SubscriberStatus.new.value
    try:
        return SubscriberStatus(raw)
    except ValueError:
        return SubscriberStatus.new


def _category(value: Any) -> SubscriberCategory:
    raw = _text(value).lower() or SubscriberCategory.residential.value
    try:
        return SubscriberCategory(raw)
    except ValueError:
        return SubscriberCategory.residential


def _crm_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    result = dict(metadata)
    for key in (
        "crm_person_id",
        "crm_project_id",
        "crm_quote_id",
        "crm_sales_order_id",
    ):
        value = _text(payload.get(key))
        if value:
            result[key] = value
    result.setdefault("source", _text(payload.get("source")) or "dotmac_omni")
    result["synced_at"] = datetime.now(UTC).isoformat()
    category = _text(
        payload.get("subscriber_category") or metadata.get("subscriber_category")
    )
    if category:
        result["subscriber_category"] = category
    return result


def _normalized_name(subscriber: Subscriber) -> str:
    value = (
        _text(subscriber.display_name)
        or _text(getattr(subscriber, "name", ""))
        or " ".join(
            part
            for part in (_text(subscriber.first_name), _text(subscriber.last_name))
            if part
        )
    )
    return " ".join(value.lower().split())


def _matching_metadata_clause(metadata: dict[str, Any]):
    # Match only on identifiers that are 1:1 with a single CRM customer:
    # the person, and the order/quote that belongs to one customer. crm_person_id
    # is the strongest identity, so when present we match on it alone. We
    # deliberately do NOT match on crm_project_id — a project can span multiple
    # customers, so an OR on it could merge two distinct customers (overwriting
    # one with the other's name/email/phone on a later webhook).
    person_id = _text(metadata.get("crm_person_id"))
    if person_id:
        return Subscriber.metadata_["crm_person_id"].as_string() == person_id

    clauses = []
    for key in ("crm_sales_order_id", "crm_quote_id"):
        value = _text(metadata.get(key))
        if value:
            clauses.append(Subscriber.metadata_[key].as_string() == value)
    return or_(*clauses) if clauses else None


def _find_existing_customer(
    db: Session, payload: dict[str, Any], metadata: dict[str, Any], display_name: str
) -> Subscriber | None:
    clause = _matching_metadata_clause(metadata)
    if clause is not None:
        existing = db.query(Subscriber).filter(clause).first()
        if existing is not None:
            return existing

    expected_name = " ".join(display_name.lower().split())
    candidates = []
    email = _text(payload.get("email")).lower()
    phone = _text(payload.get("phone"))
    if email:
        candidates.extend(
            db.query(Subscriber).filter(Subscriber.email.ilike(email)).limit(10).all()
        )
    if phone:
        candidates.extend(
            db.query(Subscriber).filter(Subscriber.phone == phone).limit(10).all()
        )
    seen: set[str] = set()
    for subscriber in candidates:
        sid = str(subscriber.id)
        if sid in seen:
            continue
        seen.add(sid)
        if expected_name and _normalized_name(subscriber) == expected_name:
            return subscriber
    return None


def _record_identity_overwrite_audit(
    db: Session,
    *,
    subscriber: Subscriber,
    changes: dict[str, dict[str, str | None]],
    metadata: dict[str, Any],
) -> None:
    if not changes:
        return
    audit_service.audit_events.create(
        db=db,
        payload=AuditEventCreate(
            actor_type=AuditActorType.service,
            actor_id="crm_webhook",
            action="crm_customer_identity_update",
            entity_type="subscriber",
            entity_id=str(subscriber.id),
            status_code=200,
            is_success=True,
            metadata_={
                "source": "crm_customer_webhook",
                "crm_person_id": metadata.get("crm_person_id"),
                "crm_quote_id": metadata.get("crm_quote_id"),
                "crm_sales_order_id": metadata.get("crm_sales_order_id"),
                "changes": changes,
            },
        ),
    )


def _track_change(
    changes: dict[str, dict[str, str | None]],
    field: str,
    before: Any,
    after: Any,
) -> None:
    before_text = None if before is None else str(before)
    after_text = None if after is None else str(after)
    if before_text != after_text:
        changes[field] = {"old": before_text, "new": after_text}


def _enum_value(value: Any) -> str | None:
    return getattr(value, "value", None) if value is not None else None


def _update_existing_customer(
    db: Session,
    subscriber: Subscriber,
    payload: dict[str, Any],
    metadata: dict[str, Any],
) -> Subscriber:
    changes: dict[str, dict[str, str | None]] = {}
    first_name, last_name, display_name = _name_parts(payload)
    if first_name:
        _track_change(changes, "first_name", subscriber.first_name, first_name)
        subscriber.first_name = first_name
    if last_name:
        _track_change(changes, "last_name", subscriber.last_name, last_name)
        subscriber.last_name = last_name
    if display_name:
        _track_change(changes, "display_name", subscriber.display_name, display_name)
        subscriber.display_name = display_name
    if _text(payload.get("email")):
        email = _text(payload.get("email"))
        _track_change(changes, "email", subscriber.email, email)
        subscriber.email = email
    if _text(payload.get("phone")):
        phone = _text(payload.get("phone"))
        _track_change(changes, "phone", subscriber.phone, phone)
        subscriber.phone = phone
    for key, value in _address_fields(payload).items():
        if value:
            _track_change(changes, key, getattr(subscriber, key), value)
            setattr(subscriber, key, value)
    status_value = _status(payload.get("status"))
    _track_change(changes, "status", _enum_value(subscriber.status), status_value.value)
    subscriber.status = status_value
    category_value = _category(
        payload.get("subscriber_category") or metadata.get("subscriber_category")
    )
    _track_change(
        changes,
        "category",
        _enum_value(subscriber.category),
        category_value.value,
    )
    subscriber.category = category_value
    merged = dict(subscriber.metadata_ or {})
    merged.update(metadata)
    subscriber.metadata_ = merged
    db.commit()
    db.refresh(subscriber)
    _record_identity_overwrite_audit(
        db,
        subscriber=subscriber,
        changes=changes,
        metadata=metadata,
    )
    return subscriber


def _create_customer_from_crm(
    db: Session, payload: dict[str, Any], metadata: dict[str, Any]
) -> Subscriber:
    first_name, last_name, display_name = _name_parts(payload)
    if not first_name or not last_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Customer name is required.",
        )
    email = _text(payload.get("email"))
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Customer email is required.",
        )
    create_payload = SubscriberCreate(
        first_name=first_name,
        last_name=last_name,
        display_name=display_name or None,
        email=email,
        phone=_text(payload.get("phone")) or None,
        status=_status(payload.get("status")),
        category=_category(
            payload.get("subscriber_category") or metadata.get("subscriber_category")
        ),
        metadata_=metadata,
        **_address_fields(payload),
    )
    return subscriber_service.subscribers.create(db, create_payload)


def _customer_response(subscriber: Subscriber) -> dict[str, Any]:
    subscriber_number = _text(subscriber.subscriber_number) or str(subscriber.id)
    return {
        "id": str(subscriber.id),
        "subscriber_id": subscriber_number,
        "subscriber_number": subscriber.subscriber_number,
        "account_number": subscriber.account_number,
    }


def upsert_customer_from_payload(
    db: Session, payload: dict[str, Any]
) -> dict[str, Any]:
    metadata = _crm_metadata(payload)
    _, _, display_name = _name_parts(payload)
    existing = _find_existing_customer(db, payload, metadata, display_name)
    subscriber = (
        _update_existing_customer(db, existing, payload, metadata)
        if existing is not None
        else _create_customer_from_crm(db, payload, metadata)
    )
    return _customer_response(subscriber)
