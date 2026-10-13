"""Admin projection for reviewed next-period subscription invoicing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.catalog import BillingMode
from app.services.advance_renewal_invoicing import (
    SUBSCRIPTION_SERVICE_INVOICE_FORM,
    ManualSubscriptionServiceInvoicePreview,
    ManualSubscriptionServiceInvoiceQuery,
    preview_manual_subscription_service_invoice,
)
from app.services.display_format import format_money, format_timestamp
from app.services.form_contracts import FormConsequence, FormPrerequisite


@dataclass(frozen=True, slots=True)
class SubscriptionServiceInvoiceFormState:
    key: str
    title: str
    entity: str
    prerequisites: tuple[FormPrerequisite, ...]
    unmet_prerequisites: tuple[FormPrerequisite, ...]
    consequences: tuple[FormConsequence, ...]
    submittable: bool


@dataclass(frozen=True, slots=True)
class SubscriptionServiceInvoicePage:
    preview: ManualSubscriptionServiceInvoicePreview
    account_label: str
    period_start_display: str
    period_end_display: str
    subtotal_display: str
    tax_total_display: str
    total_display: str
    billing_mode_display: str
    settlement_consequence: str
    form_contract: SubscriptionServiceInvoiceFormState


def build_subscription_service_invoice_page(
    db: Session,
    *,
    account_id: UUID,
    subscription_id: UUID,
    evaluated_at: datetime,
) -> SubscriptionServiceInvoicePage:
    preview = preview_manual_subscription_service_invoice(
        db,
        ManualSubscriptionServiceInvoiceQuery(
            account_id=account_id,
            subscription_id=subscription_id,
            evaluated_at=evaluated_at,
        ),
    )
    is_prepaid = preview.billing_mode is BillingMode.prepaid
    prerequisites = (
        FormPrerequisite(
            key="active_subscription",
            label="The subscription is active and belongs to this account.",
            met=True,
        ),
        FormPrerequisite(
            key="exact_next_period",
            label="The period starts at the current billing anchor and has no active invoice.",
            met=True,
        ),
        FormPrerequisite(
            key="coverage_evidence",
            label=(
                "Current funded coverage agrees with the billing anchor."
                if is_prepaid
                else "The current billing anchor agrees with the charge preview."
            ),
            met=True,
        ),
    )
    unmet_prerequisites = tuple(item for item in prerequisites if not item.met)
    form_contract = SubscriptionServiceInvoiceFormState(
        key=SUBSCRIPTION_SERVICE_INVOICE_FORM.key,
        title=SUBSCRIPTION_SERVICE_INVOICE_FORM.title,
        entity=SUBSCRIPTION_SERVICE_INVOICE_FORM.entity,
        prerequisites=prerequisites,
        unmet_prerequisites=unmet_prerequisites,
        consequences=SUBSCRIPTION_SERVICE_INVOICE_FORM.consequences,
        submittable=not unmet_prerequisites,
    )
    account_label = preview.account_name
    if preview.account_number:
        account_label = f"{account_label} · {preview.account_number}"
    return SubscriptionServiceInvoicePage(
        preview=preview,
        account_label=account_label,
        period_start_display=format_timestamp(
            preview.period_start, db, fmt="%d %b %Y %H:%M"
        ),
        period_end_display=format_timestamp(
            preview.period_end, db, fmt="%d %b %Y %H:%M"
        ),
        subtotal_display=format_money(preview.subtotal, currency=preview.currency),
        tax_total_display=format_money(preview.tax_total, currency=preview.currency),
        total_display=format_money(preview.total, currency=preview.currency),
        billing_mode_display=preview.billing_mode.value.title(),
        settlement_consequence=(
            "Once this invoice is fully paid, settlement creates one entitlement for the exact period shown. Replays do not create a second entitlement."
            if is_prepaid
            else "Payment settles this receivable. The invoice records the subscription and service period but does not create a prepaid entitlement."
        ),
        form_contract=form_contract,
    )
