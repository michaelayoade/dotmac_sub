"""Vendor-facing projection of refreshed ERP accounts-payable observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from app.services.status_presentation import (
    erp_supplier_invoice_status_presentation,
)
from app.services.ui_contracts import StateValue

ERP_PAYMENT_OBSERVATION_MAX_AGE = timedelta(minutes=15)


class VendorPaymentObservation(Protocol):
    currency: str
    erp_purchase_invoice_id: str | None
    erp_purchase_invoice_status: str | None
    erp_purchase_invoice_total_amount: Decimal | None
    erp_purchase_invoice_amount_paid: Decimal | None
    erp_purchase_invoice_balance_due: Decimal | None
    erp_purchase_invoice_status_observed_at: datetime | None
    erp_purchase_invoice_status_source_updated_at: datetime | None
    erp_purchase_invoice_status_error: str | None


@dataclass(frozen=True, slots=True)
class VendorPaymentProjection:
    """Status and amounts with explicit availability and freshness."""

    status: StateValue
    total_amount: StateValue
    amount_paid: StateValue
    balance_due: StateValue
    currency: str
    detail: str
    source_updated_at: datetime | None = None


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    # SQLite drops timezone metadata in tests; production PostgreSQL preserves
    # the UTC offset written by the reconciler.
    return value.replace(tzinfo=UTC)


def _observed_state(value: object, *, stale: bool, observed_at: datetime) -> StateValue:
    if stale:
        return StateValue.stale(value, as_of=observed_at)
    return StateValue.present(value, as_of=observed_at)


def project_vendor_payment_status(
    invoice: VendorPaymentObservation,
    *,
    now: datetime | None = None,
) -> VendorPaymentProjection:
    """Render payment truth only when the ERP refresher has observed it."""
    observed_at = _aware(
        getattr(invoice, "erp_purchase_invoice_status_observed_at", None)
    )
    error = str(
        getattr(invoice, "erp_purchase_invoice_status_error", None) or ""
    ).strip()
    currency = str(invoice.currency or "NGN").upper()
    if observed_at is None:
        absent_state_factory = StateValue.unavailable if error else StateValue.unknown
        detail = (
            "ERP payment status is temporarily unavailable."
            if error
            else (
                "Waiting for ERP payment status."
                if getattr(invoice, "erp_purchase_invoice_id", None)
                else "Waiting for this invoice to be created in ERP."
            )
        )
        return VendorPaymentProjection(
            status=absent_state_factory(),
            total_amount=absent_state_factory(),
            amount_paid=absent_state_factory(),
            balance_due=absent_state_factory(),
            currency=currency,
            detail=detail,
        )

    total_amount: Decimal | None = getattr(
        invoice, "erp_purchase_invoice_total_amount", None
    )
    amount_paid: Decimal | None = getattr(
        invoice, "erp_purchase_invoice_amount_paid", None
    )
    balance_due: Decimal | None = getattr(
        invoice, "erp_purchase_invoice_balance_due", None
    )
    status = getattr(invoice, "erp_purchase_invoice_status", None)
    if not status or total_amount is None or amount_paid is None or balance_due is None:
        return VendorPaymentProjection(
            status=StateValue.unavailable(),
            total_amount=StateValue.unavailable(),
            amount_paid=StateValue.unavailable(),
            balance_due=StateValue.unavailable(),
            currency=currency,
            detail="ERP returned an incomplete payment observation.",
        )

    current_time = _aware(now) or datetime.now(UTC)
    stale = bool(error) or current_time - observed_at > ERP_PAYMENT_OBSERVATION_MAX_AGE
    presentation = erp_supplier_invoice_status_presentation(status)
    return VendorPaymentProjection(
        status=_observed_state(presentation, stale=stale, observed_at=observed_at),
        total_amount=_observed_state(
            Decimal(total_amount), stale=stale, observed_at=observed_at
        ),
        amount_paid=_observed_state(
            Decimal(amount_paid), stale=stale, observed_at=observed_at
        ),
        balance_due=_observed_state(
            Decimal(balance_due), stale=stale, observed_at=observed_at
        ),
        currency=currency,
        detail=(
            "Last known ERP payment status; refresh is delayed."
            if stale
            else "Payment status confirmed by ERP."
        ),
        source_updated_at=_aware(
            getattr(invoice, "erp_purchase_invoice_status_source_updated_at", None)
        ),
    )
