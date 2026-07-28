"""Vendor-facing projection of replaceable payables observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from app.services.status_presentation import supplier_invoice_status_presentation
from app.services.ui_contracts import StateValue

PAYMENT_OBSERVATION_MAX_AGE = timedelta(minutes=15)
# Compatibility for callers of the pre-boundary projection API.
ERP_PAYMENT_OBSERVATION_MAX_AGE = PAYMENT_OBSERVATION_MAX_AGE


class VendorPaymentObservation(Protocol):
    currency: str
    payables_document_reference: str | None
    payment_status: str | None
    payment_total_amount: Decimal | None
    payment_amount_paid: Decimal | None
    payment_balance_due: Decimal | None
    payment_observed_at: datetime | None
    payment_source_updated_at: datetime | None
    payment_observation_error: str | None


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
    """Render payment truth only from a timestamped payables observation."""
    observed_at = _aware(getattr(invoice, "payment_observed_at", None))
    error = str(getattr(invoice, "payment_observation_error", None) or "").strip()
    currency = str(invoice.currency or "NGN").upper()
    if observed_at is None:
        absent_state_factory = StateValue.unavailable if error else StateValue.unknown
        detail = (
            "Payment status is temporarily unavailable."
            if error
            else (
                "Waiting for payment status."
                if getattr(invoice, "payables_document_reference", None)
                else "Waiting for this invoice to reach the payables system."
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

    total_amount: Decimal | None = getattr(invoice, "payment_total_amount", None)
    amount_paid: Decimal | None = getattr(invoice, "payment_amount_paid", None)
    balance_due: Decimal | None = getattr(invoice, "payment_balance_due", None)
    status = getattr(invoice, "payment_status", None)
    if not status or total_amount is None or amount_paid is None or balance_due is None:
        return VendorPaymentProjection(
            status=StateValue.unavailable(),
            total_amount=StateValue.unavailable(),
            amount_paid=StateValue.unavailable(),
            balance_due=StateValue.unavailable(),
            currency=currency,
            detail="The payables source returned an incomplete observation.",
        )

    current_time = _aware(now) or datetime.now(UTC)
    stale = bool(error) or current_time - observed_at > PAYMENT_OBSERVATION_MAX_AGE
    presentation = supplier_invoice_status_presentation(status)
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
            "Last known payment status; refresh is delayed."
            if stale
            else "Payment status confirmed by the payables source."
        ),
        source_updated_at=_aware(getattr(invoice, "payment_source_updated_at", None)),
    )
