"""Field validation API endpoints for real-time form validation."""

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.subscriber import Subscriber
from app.models.subscriber import Organization
from app.services.auth_dependencies import require_user_auth


router = APIRouter(prefix="/validation", tags=["validation"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class FieldValidationRequest(BaseModel):
    """Request body for single field validation."""
    field: str
    value: str
    context: Optional[dict] = None  # Additional context (e.g., entity ID for update)


class FieldValidationResponse(BaseModel):
    """Response for field validation."""
    valid: bool
    message: Optional[str] = None
    field: str


class FormValidationRequest(BaseModel):
    """Request body for full form validation."""
    fields: dict[str, str]
    form_type: str
    context: Optional[dict] = None


class FormValidationResponse(BaseModel):
    """Response for form validation."""
    valid: bool
    errors: dict[str, str]


# Validation rules and patterns
EMAIL_PATTERN = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
PHONE_PATTERN = re.compile(r'^\+?[0-9\s\-\(\)\.]{7,20}$')
URL_PATTERN = re.compile(
    r'^https?://'
    r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'
    r'localhost|'
    r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
    r'(?::\d+)?'
    r'(?:/?|[/?]\S+)$',
    re.IGNORECASE
)
CURRENCY_PATTERN = re.compile(r'^-?\d+(?:[,.]\d{1,2})?$')


def validate_email_format(value: str) -> tuple[bool, Optional[str]]:
    """Validate email format."""
    if not value:
        return True, None
    if not EMAIL_PATTERN.match(value):
        return False, "Please enter a valid email address"
    return True, None


def validate_phone_format(value: str) -> tuple[bool, Optional[str]]:
    """Validate phone number format."""
    if not value:
        return True, None
    cleaned = re.sub(r'[\s\-\(\)\.]', '', value)
    if not re.match(r'^\+?[0-9]{7,15}$', cleaned):
        return False, "Please enter a valid phone number"
    return True, None


def validate_url_format(value: str) -> tuple[bool, Optional[str]]:
    """Validate URL format."""
    if not value:
        return True, None
    if not URL_PATTERN.match(value):
        return False, "Please enter a valid URL"
    return True, None


def validate_currency_format(value: str) -> tuple[bool, Optional[str]]:
    """Validate currency amount format."""
    if not value:
        return True, None
    cleaned = re.sub(r'[\s,]', '', value)
    if not CURRENCY_PATTERN.match(cleaned):
        return False, "Please enter a valid currency amount"
    return True, None


def validate_required(value: str) -> tuple[bool, Optional[str]]:
    """Validate required field."""
    if not value or not value.strip():
        return False, "This field is required"
    return True, None


def validate_min_length(value: str, min_len: int) -> tuple[bool, Optional[str]]:
    """Validate minimum length."""
    if value and len(value) < min_len:
        return False, f"Must be at least {min_len} characters"
    return True, None


def validate_max_length(value: str, max_len: int) -> tuple[bool, Optional[str]]:
    """Validate maximum length."""
    if value and len(value) > max_len:
        return False, f"Must be no more than {max_len} characters"
    return True, None


async def validate_email_unique(
    db: Session,
    email: str,
    exclude_id: Optional[str] = None
) -> tuple[bool, Optional[str]]:
    """Check if email is unique in the database."""
    if not email:
        return True, None

    query = db.query(Subscriber).filter(func.lower(Subscriber.email) == email.lower())
    if exclude_id:
        query = query.filter(Subscriber.id != exclude_id)

    exists = query.first() is not None
    if exists:
        return False, "This email is already in use"
    return True, None


async def validate_org_name_unique(
    db: Session,
    name: str,
    exclude_id: Optional[str] = None
) -> tuple[bool, Optional[str]]:
    """Check if organization name is unique."""
    if not name:
        return True, None

    query = db.query(Organization).filter(func.lower(Organization.name) == name.lower())
    if exclude_id:
        query = query.filter(Organization.id != exclude_id)

    exists = query.first() is not None
    if exists:
        return False, "An organization with this name already exists"
    return True, None


@router.post("/field", response_model=FieldValidationResponse)
async def validate_field(
    request: FieldValidationRequest,
    db: Session = Depends(get_db),
    _user=Depends(require_user_auth)
):
    """
    Validate a single form field.

    Supports various validation rules based on field name:
    - email: Format validation + uniqueness check
    - phone: Phone number format
    - url/website: URL format
    - currency/amount: Currency format
    - name fields: Required + length constraints
    """
    field = request.field.lower()
    value = request.value
    context = request.context or {}
    exclude_id = context.get('exclude_id')

    # Email validation
    if 'email' in field:
        valid, message = validate_email_format(value)
        if not valid:
            return FieldValidationResponse(valid=False, message=message, field=request.field)

        # Check uniqueness
        valid, message = await validate_email_unique(db, value, exclude_id)
        if not valid:
            return FieldValidationResponse(valid=False, message=message, field=request.field)

    # Phone validation
    elif 'phone' in field or 'tel' in field:
        valid, message = validate_phone_format(value)
        if not valid:
            return FieldValidationResponse(valid=False, message=message, field=request.field)

    # URL validation
    elif 'url' in field or 'website' in field:
        valid, message = validate_url_format(value)
        if not valid:
            return FieldValidationResponse(valid=False, message=message, field=request.field)

    # Currency validation
    elif 'amount' in field or 'price' in field or 'currency' in field:
        valid, message = validate_currency_format(value)
        if not valid:
            return FieldValidationResponse(valid=False, message=message, field=request.field)

    # Organization name uniqueness
    elif field == 'name' and context.get('form_type') == 'organization':
        valid, message = await validate_org_name_unique(db, value, exclude_id)
        if not valid:
            return FieldValidationResponse(valid=False, message=message, field=request.field)

    return FieldValidationResponse(valid=True, message=None, field=request.field)


@router.post("/form/{form_type}", response_model=FormValidationResponse)
async def validate_form(
    form_type: str,
    request: FormValidationRequest,
    db: Session = Depends(get_db),
    _user=Depends(require_user_auth)
):
    """
    Validate an entire form.

    Supported form types:
    - person: Person/individual customer form
    - organization: Organization/business form
    - invoice: Invoice form
    """
    errors = {}
    context = request.context or {}
    context['form_type'] = form_type

    if form_type == 'person':
        # Required fields
        for field in ['first_name', 'last_name', 'email']:
            if field in request.fields:
                valid, message = validate_required(request.fields[field])
                if not valid:
                    errors[field] = message

        # Email format and uniqueness
        if 'email' in request.fields and not errors.get('email'):
            valid, message = validate_email_format(request.fields['email'])
            if not valid:
                errors['email'] = message
            else:
                valid, message = await validate_email_unique(
                    db, request.fields['email'], context.get('exclude_id')
                )
                if not valid:
                    errors['email'] = message

        # Phone format
        if 'phone' in request.fields:
            valid, message = validate_phone_format(request.fields['phone'])
            if not valid:
                errors['phone'] = message

    elif form_type == 'organization':
        # Required fields
        if 'name' in request.fields:
            valid, message = validate_required(request.fields['name'])
            if not valid:
                errors['name'] = message
            else:
                valid, message = await validate_org_name_unique(
                    db, request.fields['name'], context.get('exclude_id')
                )
                if not valid:
                    errors['name'] = message

        # Website URL
        if 'website' in request.fields:
            valid, message = validate_url_format(request.fields['website'])
            if not valid:
                errors['website'] = message

    elif form_type == 'invoice':
        # Required fields
        if 'account_id' in request.fields:
            valid, message = validate_required(request.fields['account_id'])
            if not valid:
                errors['account_id'] = message

    return FormValidationResponse(
        valid=len(errors) == 0,
        errors=errors
    )
