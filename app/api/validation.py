"""Field validation API endpoints for real-time form validation."""


from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import validation_api as validation_api_service
from app.services.auth_dependencies import require_user_auth

router = APIRouter(prefix="/validation", tags=["validation"])


class FieldValidationRequest(BaseModel):
    """Request body for single field validation."""
    field: str
    value: str
    context: dict | None = None  # Additional context (e.g., entity ID for update)


class FieldValidationResponse(BaseModel):
    """Response for field validation."""
    valid: bool
    message: str | None = None
    field: str


class FormValidationRequest(BaseModel):
    """Request body for full form validation."""
    fields: dict[str, str]
    form_type: str
    context: dict | None = None


class FormValidationResponse(BaseModel):
    """Response for form validation."""
    valid: bool
    errors: dict[str, str]


@router.post("/field", response_model=FieldValidationResponse)
def validate_field(
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
    valid, message = validation_api_service.validate_field(
        db,
        field=request.field,
        value=request.value,
        context=request.context,
    )
    return FieldValidationResponse(valid=valid, message=message, field=request.field)


@router.post("/form/{form_type}", response_model=FormValidationResponse)
def validate_form(
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
    errors = validation_api_service.validate_form(
        db,
        form_type=form_type,
        fields=request.fields,
        context=request.context,
    )
    return FormValidationResponse(valid=len(errors) == 0, errors=errors)
