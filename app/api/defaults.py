"""Smart Defaults API endpoints."""

from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.auth_dependencies import require_user_auth
from app.services.smart_defaults import SmartDefaultsService

router = APIRouter(prefix="/defaults", tags=["defaults"])


class InvoiceDefaultsResponse(BaseModel):
    """Response model for invoice defaults."""
    currency: str
    payment_terms_days: int
    issued_at: str
    due_at: str
    status: str


class CustomerDefaultsResponse(BaseModel):
    """Response model for customer defaults."""
    status: str
    is_active: bool
    country_code: str
    locale: str
    gender: str | None = None
    email_verified: bool | None = None
    marketing_opt_in: bool | None = None


class SubscriptionDefaultsResponse(BaseModel):
    """Response model for subscription defaults."""
    billing_cycle: str
    currency: str
    status: str
    start_date: str
    auto_renew: bool


class CurrencySettingsResponse(BaseModel):
    """Response model for currency settings."""
    default_currency: str
    supported_currencies: list[str]
    decimal_places: int


class DueDateCalculationRequest(BaseModel):
    """Request body for due date calculation."""
    issued_at: str | None = None
    payment_terms_days: int | None = None


class DueDateCalculationResponse(BaseModel):
    """Response for due date calculation."""
    issued_at: str
    payment_terms_days: int
    due_at: str


@router.get("/invoice", response_model=InvoiceDefaultsResponse)
def get_invoice_defaults(
    db: Session = Depends(get_db),
    _user=Depends(require_user_auth)
):
    """
    Get default values for creating a new invoice.

    Returns default currency, payment terms, dates, and status
    based on domain settings.
    """
    service = SmartDefaultsService(db)
    defaults = service.get_invoice_defaults()
    return InvoiceDefaultsResponse(**defaults)


@router.get("/customer/{customer_type}", response_model=CustomerDefaultsResponse)
def get_customer_defaults(
    customer_type: str,
    db: Session = Depends(get_db),
    _user=Depends(require_user_auth)
):
    """
    Get default values for creating a new customer.

    Args:
        customer_type: Either 'person' or 'organization'

    Returns default status, country, locale, and type-specific defaults.
    """
    if customer_type not in ("person", "organization"):
        customer_type = "person"

    service = SmartDefaultsService(db)
    defaults = service.get_customer_defaults(customer_type)
    return CustomerDefaultsResponse(**defaults)


@router.get("/subscription", response_model=SubscriptionDefaultsResponse)
def get_subscription_defaults(
    db: Session = Depends(get_db),
    _user=Depends(require_user_auth)
):
    """
    Get default values for creating a new subscription.

    Returns default billing cycle, currency, status, and dates.
    """
    service = SmartDefaultsService(db)
    defaults = service.get_subscription_defaults()
    return SubscriptionDefaultsResponse(**defaults)


@router.get("/currency", response_model=CurrencySettingsResponse)
def get_currency_settings(
    db: Session = Depends(get_db),
    _user=Depends(require_user_auth)
):
    """
    Get currency-related settings.

    Returns default currency, supported currencies, and decimal places.
    """
    service = SmartDefaultsService(db)
    settings = service.get_currency_settings()
    return CurrencySettingsResponse(**settings)


@router.post("/calculate-due-date", response_model=DueDateCalculationResponse)
def calculate_due_date(
    request: DueDateCalculationRequest,
    db: Session = Depends(get_db),
    _user=Depends(require_user_auth)
):
    """
    Calculate the due date based on issue date and payment terms.

    If issued_at is not provided, uses today's date.
    If payment_terms_days is not provided, uses the default from settings.
    """
    service = SmartDefaultsService(db)

    issued_at = None
    if request.issued_at:
        issued_at = date.fromisoformat(request.issued_at)

    result = service.calculate_due_date_detail(
        issued_at=issued_at,
        payment_terms_days=request.payment_terms_days,
    )
    return DueDateCalculationResponse(**result)
