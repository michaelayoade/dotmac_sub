"""Service helpers for billing invoice form pages."""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from app.services import billing as billing_service
from app.services import display_format
from app.services import subscriber as subscriber_service
from app.services import web_billing_customers as web_billing_customers_service
from app.services import web_billing_invoices as web_billing_invoices_service
from app.services.action_forms import ActionConfirmation, ActionForm, ActionHiddenValue
from app.services.billing_settings import resolve_payment_due_days
from app.services.future_invoice_authoring import (
    CatalogueChoice,
    CatalogueQuery,
    FutureInvoiceLinePreview,
    FutureInvoicePeriodChoice,
    FutureInvoicePeriodsQuery,
    PreviewFutureInvoiceLineQuery,
    catalogue_currencies,
)
from app.timezone import APP_TIMEZONE

logger = logging.getLogger(__name__)


def _build_invoice_form_config(
    invoice,
    tax_rates_json: list | None,
    existing_line_items: list | None,
    payment_terms_days: int = 30,
) -> dict:
    currency = ""
    if invoice and invoice.currency:
        currency = str(invoice.currency).upper()
    return {
        "accountId": str(invoice.account_id) if invoice and invoice.account_id else "",
        "invoiceNumber": invoice.invoice_number
        if invoice and invoice.invoice_number
        else "",
        "status": invoice.status.value if invoice and invoice.status else "draft",
        "currency": currency or "NGN",
        "issuedAt": invoice.issued_at.strftime("%Y-%m-%d")
        if invoice and invoice.issued_at
        else "",
        "dueAt": invoice.due_at.strftime("%Y-%m-%d")
        if invoice and invoice.due_at
        else "",
        "memo": invoice.memo if invoice and invoice.memo else "",
        "taxRates": tax_rates_json or [],
        "lineItems": existing_line_items or [],
        "invoiceId": str(invoice.id) if invoice else "",
        "paymentTermsDays": payment_terms_days,
        "initialSelections": [
            {
                "line_id": str(line.id),
                "selection": line.metadata_["future_invoice"]["selection"],
            }
            for line in (invoice.lines if invoice else [])
            if line.is_active
            and isinstance(line.metadata_, dict)
            and isinstance(line.metadata_.get("future_invoice"), dict)
            and isinstance(line.metadata_["future_invoice"].get("selection"), dict)
        ],
        "discountType": getattr(invoice, "discount_type", None) if invoice else "",
        "discountValue": (
            float(getattr(invoice, "discount_value", 0))
            if invoice and getattr(invoice, "discount_value", None) is not None
            else ""
        ),
        "discountReason": getattr(invoice, "discount_reason", None) if invoice else "",
        "discountLocked": bool(
            invoice and getattr(invoice, "discount_source", None) == "quote"
        ),
    }


def resolve_selected_account(db: Session, account_id: str | None):
    if not account_id:
        return None
    try:
        return subscriber_service.accounts.get(db=db, account_id=account_id)
    except Exception:
        return None


def new_form_state(db: Session, *, account_id: str | None) -> dict[str, object]:
    selected_account = resolve_selected_account(db, account_id)
    tax_rates = web_billing_invoices_service.load_tax_rates(db)
    tax_rates_json = [
        {"id": str(rate.id), "name": rate.name, "rate": float(rate.rate or 0)}
        for rate in tax_rates or []
    ]
    payment_due_days = resolve_payment_due_days(db, subscriber=selected_account)
    invoice_config = _build_invoice_form_config(
        None,
        tax_rates_json,
        [
            {
                "description": "",
                "quantity": 1,
                "unit_price": 0,
                "tax_rate_id": None,
            }
        ],
        payment_due_days,
    )
    if selected_account:
        invoice_config["accountId"] = str(selected_account.id)

    default_currency = display_format.default_currency(db)
    invoice_config["currency"] = default_currency
    today = datetime.now(UTC).date()

    return {
        "accounts": None,
        "tax_rates": tax_rates,
        "tax_rates_json": tax_rates_json,
        "invoice_config": invoice_config,
        "default_issue_date": today.strftime("%Y-%m-%d"),
        "default_due_date": (today + timedelta(days=payment_due_days)).strftime(
            "%Y-%m-%d"
        ),
        "default_currency": default_currency,
        "currency_options": tuple(
            sorted(set(catalogue_currencies(db)) | {default_currency})
        ),
        "account_locked": bool(selected_account),
        "account_label": web_billing_customers_service.account_label(selected_account)
        if selected_account
        else None,
        "account_number": selected_account.account_number if selected_account else None,
        "selected_account_id": str(selected_account.id) if selected_account else None,
        "account_x_model": "accountId",
        "draft_idempotency_key": secrets.token_urlsafe(24),
        "discount_applied_date": datetime.now(APP_TIMEZONE).date().isoformat(),
    }


def edit_form_state(db: Session, *, invoice_id: str) -> dict[str, object] | None:
    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if not invoice:
        return None
    selected_account = invoice.account
    if not selected_account and invoice.account_id:
        selected_account = resolve_selected_account(db, str(invoice.account_id))
    tax_rates = web_billing_invoices_service.load_tax_rates(db)
    existing_line_items = [
        {
            "id": str(line.id),
            "description": line.description,
            "quantity": float(line.quantity or 1),
            "unit_price": float(line.unit_price or 0),
            "tax_rate_id": str(line.tax_rate_id) if line.tax_rate_id else None,
        }
        for line in (invoice.lines or [])
        if getattr(line, "is_active", True)
    ]
    tax_rates_json = [
        {"id": str(rate.id), "name": rate.name, "rate": float(rate.rate or 0)}
        for rate in tax_rates or []
    ]
    payment_due_days = resolve_payment_due_days(db, subscriber=selected_account)
    invoice_config = _build_invoice_form_config(
        invoice, tax_rates_json, existing_line_items, payment_due_days
    )
    discount_applied_at = getattr(invoice, "discount_applied_at", None)
    if discount_applied_at is not None:
        if discount_applied_at.tzinfo is None:
            discount_applied_at = discount_applied_at.replace(tzinfo=UTC)
        discount_applied_date = discount_applied_at.astimezone(APP_TIMEZONE).date()
    else:
        discount_applied_date = datetime.now(APP_TIMEZONE).date()
    return {
        "invoice": invoice,
        "accounts": None,
        "tax_rates": tax_rates,
        "tax_rates_json": tax_rates_json,
        "existing_line_items": existing_line_items,
        "currency_options": tuple(
            sorted(set(catalogue_currencies(db)) | {invoice.currency})
        ),
        "default_due_date": invoice.due_at.astimezone(
            display_format.display_timezone(db)
        )
        .date()
        .isoformat()
        if invoice.due_at
        else "",
        "invoice_config": invoice_config,
        "show_line_items": False,
        "account_locked": True,
        "account_label": web_billing_customers_service.account_label(selected_account),
        "account_number": selected_account.account_number if selected_account else None,
        "selected_account_id": str(selected_account.id)
        if selected_account
        else str(invoice.account_id),
        "account_x_model": "accountId",
        "draft_idempotency_key": secrets.token_urlsafe(24),
        "discount_applied_date": discount_applied_date.isoformat(),
    }


# Catalogue controls consume these display projections rather than formatting
# financial amounts or subscription timestamps in the browser.


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogueChoiceDisplay(CatalogueChoice):
    id: str
    label: str
    amount_display: str


def catalogue_choice_display(
    db: Session, *, choice: CatalogueChoice
) -> CatalogueChoiceDisplay:
    amount = display_format.format_money(choice.amount, db=db, currency=choice.currency)
    return CatalogueChoiceDisplay(
        **{field.name: getattr(choice, field.name) for field in fields(choice)},
        id=str(choice.price_id),
        label=f"{choice.name} · {amount}",
        amount_display=amount,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class FutureInvoiceLineDisplay(FutureInvoiceLinePreview):
    total_display: str
    tax_total_display: str
    unit_price_display: str
    starts_at_display: str
    ends_at_display: str


def future_line_display(
    db: Session, *, line: FutureInvoiceLinePreview
) -> FutureInvoiceLineDisplay:
    currency = line.selection.query.currency
    return FutureInvoiceLineDisplay(
        **{field.name: getattr(line, field.name) for field in fields(line)},
        total_display=display_format.format_money(line.total, db=db, currency=currency),
        tax_total_display=display_format.format_money(
            line.tax_total, db=db, currency=currency
        ),
        unit_price_display=display_format.format_money(
            line.unit_price, db=db, currency=currency
        ),
        starts_at_display=display_format.format_timestamp(line.starts_at, db),
        ends_at_display=display_format.format_timestamp(line.ends_at, db),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class FutureInvoicePeriodDisplay(FutureInvoicePeriodChoice):
    starts_on: str


def future_period_display(
    db: Session, *, period: FutureInvoicePeriodChoice
) -> FutureInvoicePeriodDisplay:
    starts_on = (
        period.starts_at.astimezone(display_format.display_timezone(db))
        .date()
        .isoformat()
        if period.starts_at
        else ""
    )
    label = (
        f"{display_format.format_timestamp(period.starts_at, db)} → {display_format.format_timestamp(period.ends_at, db)}"
        if period.starts_at
        else period.label
    )
    values = {field.name: getattr(period, field.name) for field in fields(period)}
    values["label"] = label
    return FutureInvoicePeriodDisplay(**values, starts_on=starts_on)


@dataclass(frozen=True, slots=True)
class FutureInvoiceReviewRequest:
    account_id: UUID
    currency: str
    line_items_json: str
    draft_idempotency_key: str
    action_url: str
    due_at: str
    issued_at: str = ""
    memo: str = ""
    invoice_id: UUID | None = None
    invoice_number: str = ""


@dataclass(frozen=True, slots=True)
class FutureInvoiceReview:
    lines: tuple[FutureInvoiceLineDisplay, ...]
    total_display: str
    tax_total_display: str
    account_label: str
    action: ActionForm


def build_future_invoice_review(
    db: Session, *, query: FutureInvoiceReviewRequest
) -> FutureInvoiceReview:
    """Re-read authoritative line previews and project the shared review form."""
    from app.services.future_invoice_authoring import (
        confirm_future_invoice_line,
        selection_to_wire,
    )
    from app.services.web_billing_payment_forms import account_label
    from app.validators.forms import parse_decimal

    parsed = web_billing_invoices_service.parse_create_line_items(
        line_items_json=query.line_items_json,
        line_description=[],
        line_quantity=[],
        line_unit_price=[],
        line_tax_rate_id=[],
        parse_decimal=parse_decimal,
    )
    previews: list[FutureInvoiceLineDisplay] = []
    submitted: list[dict[str, object]] = []
    for item in parsed:
        selection = item["catalogue_selection"]
        if selection is None:
            raise ValueError("Every invoice line requires a catalogue selection.")
        preview = confirm_future_invoice_line(
            db,
            account_id=query.account_id,
            currency=query.currency,
            selection=selection,
            invoice_id=query.invoice_id,
        )
        previews.append(future_line_display(db, line=preview))
        submitted.append(
            {
                "id": str(item["line_id"]) if item["line_id"] else None,
                "description": preview.description,
                "quantity": str(preview.quantity),
                "unitPrice": str(preview.unit_price),
                "taxRateId": str(preview.tax_rate_id) if preview.tax_rate_id else None,
                "subscription_id": str(preview.subscription_id)
                if preview.subscription_id
                else None,
                "catalogue_selection": selection_to_wire(preview.selection),
            }
        )
    if not previews:
        raise ValueError(
            "Add at least one catalogue item before reviewing the invoice."
        )
    total = sum((line.total for line in previews), Decimal("0"))
    vat = sum((line.tax_total for line in previews), Decimal("0"))
    hidden = (
        ("account_id", str(query.account_id)),
        ("currency", query.currency),
        ("line_items_json", json.dumps(submitted)),
        ("draft_idempotency_key", query.draft_idempotency_key),
        ("due_at", query.due_at),
        ("issued_at", query.issued_at),
        ("memo", query.memo),
        ("invoice_number", query.invoice_number),
        ("future_confirmed", "yes"),
        ("status", "draft"),
    )
    total_display = display_format.format_money(total, db=db, currency=query.currency)
    vat_display = display_format.format_money(vat, db=db, currency=query.currency)
    account = subscriber_service.accounts.get(db=db, account_id=str(query.account_id))
    label = account_label(account)
    action = ActionForm(
        key="invoice.catalogue_draft_save",
        title="Confirm your invoice",
        description=f"{label} · {len(previews)} invoice line(s).",
        action_url=query.action_url,
        submit_label="Confirm and save draft",
        fields=(),
        hidden_values=tuple(
            ActionHiddenValue(key, value) for key, value in hidden if value
        ),
        impact=f"Invoice total {total_display}, including VAT {vat_display}. Saving reserves the selected service periods or pending renewal; it does not activate or extend service.",
        confirmation=ActionConfirmation(
            "I reviewed the customer, catalogue items and service dates",
            "Save this draft and reserve its selected periods. Availability and authoritative prices are checked again before saving.",
        ),
    )
    return FutureInvoiceReview(
        tuple(previews), total_display, vat_display, label, action
    )


@dataclass(frozen=True, slots=True)
class CatalogueTypeaheadResult:
    items: tuple[CatalogueChoiceDisplay, ...]


def catalogue_display_choices(
    db: Session, *, query: CatalogueQuery
) -> tuple[CatalogueChoiceDisplay, ...]:
    from app.services.future_invoice_authoring import catalogue_choices

    return tuple(
        catalogue_choice_display(db, choice=choice)
        for choice in catalogue_choices(db, query)
    )


def future_display_periods(
    db: Session, *, query: FutureInvoicePeriodsQuery
) -> tuple[FutureInvoicePeriodDisplay, ...]:
    from app.services.future_invoice_authoring import future_period_choices

    return tuple(
        future_period_display(db, period=period)
        for period in future_period_choices(db, query)
    )


def preview_future_display_line(
    db: Session, *, query: PreviewFutureInvoiceLineQuery
) -> FutureInvoiceLineDisplay:
    from app.services.future_invoice_authoring import preview_future_invoice_line

    return future_line_display(db, line=preview_future_invoice_line(db, query))


@dataclass(frozen=True, slots=True)
class InvoiceEditReviewRequirementQuery:
    invoice_id: UUID
    line_items_json: str | None


def catalogue_edit_requires_review(
    db: Session, *, query: InvoiceEditReviewRequirementQuery
) -> bool:
    """The UI must not fall back to an unreviewed edit for an existing catalogue draft."""
    invoice = billing_service.invoices.get(db=db, invoice_id=str(query.invoice_id))
    if invoice is not None and any(
        line.is_active and (line.metadata_ or {}).get("future_catalogue_invoice")
        for line in invoice.lines
    ):
        return True
    if not query.line_items_json:
        return False
    from app.validators.forms import parse_decimal

    lines = web_billing_invoices_service.parse_create_line_items(
        line_items_json=query.line_items_json,
        line_description=[],
        line_quantity=[],
        line_unit_price=[],
        line_tax_rate_id=[],
        parse_decimal=parse_decimal,
    )
    return any(line["catalogue_selection"] is not None for line in lines)
