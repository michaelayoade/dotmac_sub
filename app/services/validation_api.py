"""Service helpers for validation API endpoints."""

from __future__ import annotations

import re

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.subscriber import Organization, Subscriber

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PHONE_PATTERN = re.compile(r"^\+?[0-9\s\-\(\)\.]{7,20}$")
URL_PATTERN = re.compile(
    r"^https?://"
    r"(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|"
    r"localhost|"
    r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"
    r"(?::\d+)?"
    r"(?:/?|[/?]\S+)$",
    re.IGNORECASE,
)
CURRENCY_PATTERN = re.compile(r"^-?\d+(?:[,.]\d{1,2})?$")


def validate_email_format(value: str) -> tuple[bool, str | None]:
    if not value:
        return True, None
    if not EMAIL_PATTERN.match(value):
        return False, "Please enter a valid email address"
    return True, None


def validate_phone_format(value: str) -> tuple[bool, str | None]:
    if not value:
        return True, None
    cleaned = re.sub(r"[\s\-\(\)\.]", "", value)
    if not re.match(r"^\+?[0-9]{7,15}$", cleaned):
        return False, "Please enter a valid phone number"
    return True, None


def validate_url_format(value: str) -> tuple[bool, str | None]:
    if not value:
        return True, None
    if not URL_PATTERN.match(value):
        return False, "Please enter a valid URL"
    return True, None


def validate_currency_format(value: str) -> tuple[bool, str | None]:
    if not value:
        return True, None
    cleaned = re.sub(r"[\s,]", "", value)
    if not CURRENCY_PATTERN.match(cleaned):
        return False, "Please enter a valid currency amount"
    return True, None


def validate_required(value: str) -> tuple[bool, str | None]:
    if not value or not value.strip():
        return False, "This field is required"
    return True, None


def validate_email_unique(
    db: Session,
    email: str,
    exclude_id: str | None = None,
) -> tuple[bool, str | None]:
    if not email:
        return True, None
    query = db.query(Subscriber).filter(func.lower(Subscriber.email) == email.lower())
    if exclude_id:
        query = query.filter(Subscriber.id != exclude_id)
    if query.first() is not None:
        return False, "This email is already in use"
    return True, None


def validate_org_name_unique(
    db: Session,
    name: str,
    exclude_id: str | None = None,
) -> tuple[bool, str | None]:
    if not name:
        return True, None
    query = db.query(Organization).filter(func.lower(Organization.name) == name.lower())
    if exclude_id:
        query = query.filter(Organization.id != exclude_id)
    if query.first() is not None:
        return False, "An organization with this name already exists"
    return True, None


def validate_field(
    db: Session,
    *,
    field: str,
    value: str,
    context: dict | None,
) -> tuple[bool, str | None]:
    normalized_field = field.lower()
    context = context or {}
    exclude_id = context.get("exclude_id")

    if "email" in normalized_field:
        valid, message = validate_email_format(value)
        if not valid:
            return valid, message
        return validate_email_unique(db, value, exclude_id)

    if "phone" in normalized_field or "tel" in normalized_field:
        return validate_phone_format(value)

    if "url" in normalized_field or "website" in normalized_field:
        return validate_url_format(value)

    if (
        "amount" in normalized_field
        or "price" in normalized_field
        or "currency" in normalized_field
    ):
        return validate_currency_format(value)

    if normalized_field == "name" and context.get("form_type") == "organization":
        return validate_org_name_unique(db, value, exclude_id)

    return True, None


def validate_form(
    db: Session,
    *,
    form_type: str,
    fields: dict[str, str],
    context: dict | None,
) -> dict[str, str]:
    errors: dict[str, str] = {}
    context = context or {}

    if form_type == "person":
        for field in ["first_name", "last_name", "email"]:
            if field in fields:
                valid, message = validate_required(fields[field])
                if not valid:
                    errors[field] = message or "Invalid value"

        if "email" in fields and not errors.get("email"):
            valid, message = validate_email_format(fields["email"])
            if not valid:
                errors["email"] = message or "Invalid value"
            else:
                valid, message = validate_email_unique(
                    db,
                    fields["email"],
                    context.get("exclude_id"),
                )
                if not valid:
                    errors["email"] = message or "Invalid value"

        if "phone" in fields:
            valid, message = validate_phone_format(fields["phone"])
            if not valid:
                errors["phone"] = message or "Invalid value"

    elif form_type == "organization":
        if "name" in fields:
            valid, message = validate_required(fields["name"])
            if not valid:
                errors["name"] = message or "Invalid value"
            else:
                valid, message = validate_org_name_unique(
                    db,
                    fields["name"],
                    context.get("exclude_id"),
                )
                if not valid:
                    errors["name"] = message or "Invalid value"

        if "website" in fields:
            valid, message = validate_url_format(fields["website"])
            if not valid:
                errors["website"] = message or "Invalid value"

    elif form_type == "invoice":
        if "account_id" in fields:
            valid, message = validate_required(fields["account_id"])
            if not valid:
                errors["account_id"] = message or "Invalid value"

    return errors
