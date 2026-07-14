"""Tax source facts, reporting semantics, and the WHT evidence lifecycle.

Sub owns the billing facts: invoice and credit-note tax treatments, proof-backed
WHT amounts, and the official WHT evidence timeline. Dotmac ERP owns TaxCode
account mappings, double-entry journals, tax transactions, and financial
statements. This module therefore never stores account codes or creates ledger
postings.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.billing import (
    CreditNote,
    CreditNoteStatus,
    Invoice,
    InvoiceStatus,
    Payment,
)
from app.models.payment_proof import (
    WithholdingTaxRecord,
    WithholdingTaxStatus,
    WithholdingTaxTransition,
)
from app.models.subscriber import Reseller
from app.services.common import coerce_uuid, parse_date_filter
from app.services.status_presentation import (
    credit_note_status_presentation,
    invoice_status_presentation,
    withholding_tax_status_presentation,
)

TAX_REPORT_ROW_LIMIT = 200
TAX_OPERATIONS_PAGE_SIZE = 25

REPORTABLE_INVOICE_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.paid,
    InvoiceStatus.overdue,
    InvoiceStatus.written_off,
)
ADJUSTING_CREDIT_NOTE_STATUSES = (
    CreditNoteStatus.issued,
    CreditNoteStatus.partially_applied,
    CreditNoteStatus.applied,
)
OUTSTANDING_WHT_STATUSES = (
    WithholdingTaxStatus.pending,
    WithholdingTaxStatus.certified,
)

_MONEY_QUANTUM = Decimal("0.01")


class TaxAccountingError(ValueError):
    """A tax source fact or lifecycle request is invalid."""


class TaxSourceNotFound(TaxAccountingError):
    """A requested source record does not exist."""


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(_MONEY_QUANTUM)


def _currency(value: object) -> str:
    return str(value or "NGN").strip().upper() or "NGN"


def _status_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _window_filters(date_from: str | None, date_to: str | None):
    start = parse_date_filter(date_from)
    end_date = parse_date_filter(date_to)
    end = end_date + timedelta(days=1) if end_date is not None else None
    if start is not None and end is not None and end <= start:
        end = start + timedelta(days=1)
    return start, end


def _invoice_filters(date_from: str | None, date_to: str | None):
    start, end = _window_filters(date_from, date_to)
    tax_point = func.coalesce(Invoice.issued_at, Invoice.created_at)
    filters = [
        Invoice.is_active.is_(True),
        Invoice.is_proforma.is_(False),
        Invoice.tax_total > 0,
        Invoice.status.in_(REPORTABLE_INVOICE_STATUSES),
    ]
    if start is not None:
        filters.append(tax_point >= start)
    if end is not None:
        filters.append(tax_point < end)
    return tax_point, filters


def _credit_note_filters(date_from: str | None, date_to: str | None):
    start, end = _window_filters(date_from, date_to)
    tax_point = func.coalesce(CreditNote.issued_at, CreditNote.created_at)
    filters = [
        CreditNote.is_active.is_(True),
        CreditNote.tax_total > 0,
        CreditNote.status.in_(ADJUSTING_CREDIT_NOTE_STATUSES),
    ]
    if start is not None:
        filters.append(tax_point >= start)
    if end is not None:
        filters.append(tax_point < end)
    return tax_point, filters


def _wht_filters(date_from: str | None, date_to: str | None):
    start, end = _window_filters(date_from, date_to)
    filters = [WithholdingTaxRecord.wht_amount > 0]
    if start is not None:
        filters.append(WithholdingTaxRecord.created_at >= start)
    if end is not None:
        filters.append(WithholdingTaxRecord.created_at < end)
    return filters


def _output_tax_projection(db: Session, *, date_from: str | None, date_to: str | None):
    tax_point, filters = _invoice_filters(date_from, date_to)
    aggregates = db.execute(
        select(
            Invoice.currency,
            func.count(Invoice.id),
            func.sum(Invoice.tax_total),
            func.sum(Invoice.total),
        )
        .where(*filters)
        .group_by(Invoice.currency)
    ).all()
    totals = [
        {
            "currency": _currency(currency),
            "invoice_count": int(count or 0),
            "tax_amount": _money(tax),
            "gross_amount": _money(gross),
        }
        for currency, count, tax, gross in aggregates
    ]
    totals.sort(key=lambda item: str(item["currency"]))
    invoices = list(
        db.scalars(
            select(Invoice)
            .where(*filters)
            .order_by(tax_point.desc(), Invoice.id.desc())
            .limit(TAX_REPORT_ROW_LIMIT)
        ).all()
    )
    rows = [
        {
            "invoice_id": str(invoice.id),
            "invoice_number": invoice.invoice_number,
            "tax_point_at": invoice.issued_at or invoice.created_at,
            "currency": _currency(invoice.currency),
            "tax_amount": _money(invoice.tax_total),
            "gross_amount": _money(invoice.total),
            "status": _status_value(invoice.status),
            "status_presentation": invoice_status_presentation(invoice.status),
        }
        for invoice in invoices
    ]
    return rows, totals, sum(int(str(item["invoice_count"])) for item in totals)


def _credit_note_tax_projection(
    db: Session, *, date_from: str | None, date_to: str | None
):
    tax_point, filters = _credit_note_filters(date_from, date_to)
    aggregates = db.execute(
        select(
            CreditNote.currency,
            func.count(CreditNote.id),
            func.sum(CreditNote.tax_total),
            func.sum(CreditNote.total),
        )
        .where(*filters)
        .group_by(CreditNote.currency)
    ).all()
    totals = [
        {
            "currency": _currency(currency),
            "credit_note_count": int(count or 0),
            "tax_adjustment_amount": _money(tax),
            "gross_credit_amount": _money(gross),
        }
        for currency, count, tax, gross in aggregates
    ]
    totals.sort(key=lambda item: str(item["currency"]))
    notes = list(
        db.scalars(
            select(CreditNote)
            .where(*filters)
            .order_by(tax_point.desc(), CreditNote.id.desc())
            .limit(TAX_REPORT_ROW_LIMIT)
        ).all()
    )
    rows = [
        {
            "credit_note_id": str(note.id),
            "credit_number": note.credit_number,
            "recognized_at": note.issued_at or note.created_at,
            "currency": _currency(note.currency),
            "tax_adjustment_amount": _money(note.tax_total),
            "gross_credit_amount": _money(note.total),
            "status": _status_value(note.status),
            "status_presentation": credit_note_status_presentation(note.status),
        }
        for note in notes
    ]
    return rows, totals, sum(int(str(item["credit_note_count"])) for item in totals)


def _net_output_tax_totals(output_totals: list[dict], credit_totals: list[dict]):
    currencies = {str(item["currency"]) for item in [*output_totals, *credit_totals]}
    rows = []
    for currency in sorted(currencies):
        output = next(
            (item for item in output_totals if item["currency"] == currency), None
        )
        credit = next(
            (item for item in credit_totals if item["currency"] == currency), None
        )
        invoiced = _money(output["tax_amount"] if output else 0)
        adjusted = _money(credit["tax_adjustment_amount"] if credit else 0)
        rows.append(
            {
                "currency": currency,
                "invoice_count": int(output["invoice_count"] if output else 0),
                "credit_note_count": int(credit["credit_note_count"] if credit else 0),
                "output_tax_invoiced": invoiced,
                "credit_note_tax_adjustments": adjusted,
                "net_output_tax_liability": invoiced - adjusted,
            }
        )
    return rows


def _wht_projection(db: Session, *, date_from: str | None, date_to: str | None):
    filters = _wht_filters(date_from, date_to)
    aggregates = db.execute(
        select(
            WithholdingTaxRecord.currency,
            WithholdingTaxRecord.status,
            func.count(WithholdingTaxRecord.id),
            func.sum(WithholdingTaxRecord.gross_amount),
            func.sum(WithholdingTaxRecord.net_amount),
            func.sum(WithholdingTaxRecord.wht_amount),
        )
        .where(*filters)
        .group_by(WithholdingTaxRecord.currency, WithholdingTaxRecord.status)
    ).all()
    by_currency: dict[str, dict] = {}
    for currency, status, count, gross, net, wht in aggregates:
        code = _currency(currency)
        bucket = by_currency.setdefault(
            code,
            {
                "currency": code,
                "record_count": 0,
                "gross_amount": Decimal("0.00"),
                "net_cash_amount": Decimal("0.00"),
                "wht_amount": Decimal("0.00"),
                "outstanding_wht_amount": Decimal("0.00"),
                "by_status": {},
            },
        )
        amount = _money(wht)
        bucket["record_count"] += int(count or 0)
        bucket["gross_amount"] += _money(gross)
        bucket["net_cash_amount"] += _money(net)
        bucket["wht_amount"] += amount
        if status in OUTSTANDING_WHT_STATUSES:
            bucket["outstanding_wht_amount"] += amount
        status_name = _status_value(status)
        bucket["by_status"][status_name] = (
            _money(bucket["by_status"].get(status_name)) + amount
        )
    records = list(
        db.scalars(
            select(WithholdingTaxRecord)
            .where(*filters)
            .order_by(
                WithholdingTaxRecord.created_at.desc(),
                WithholdingTaxRecord.id.desc(),
            )
            .limit(TAX_REPORT_ROW_LIMIT)
        ).all()
    )
    rows = [
        {
            "record_id": str(record.id),
            "recognized_at": record.created_at,
            "currency": _currency(record.currency),
            "gross_amount": _money(record.gross_amount),
            "net_cash_amount": _money(record.net_amount),
            "wht_amount": _money(record.wht_amount),
            "wht_rate": record.wht_rate,
            "status": _status_value(record.status),
            "status_presentation": withholding_tax_status_presentation(record.status),
            "billing_account_id": str(record.billing_account_id),
            "reseller_id": str(record.reseller_id) if record.reseller_id else None,
        }
        for record in records
    ]
    totals = [by_currency[key] for key in sorted(by_currency)]
    return rows, totals, sum(item["record_count"] for item in totals)


def build_tax_report(
    db: Session, *, date_from: str | None = None, date_to: str | None = None
) -> dict[str, object]:
    """Build the read-only source-document tax register, grouped by currency."""
    invoice_rows, output_totals, invoice_count = _output_tax_projection(
        db, date_from=date_from, date_to=date_to
    )
    credit_rows, credit_totals, credit_count = _credit_note_tax_projection(
        db, date_from=date_from, date_to=date_to
    )
    wht_rows, wht_totals, wht_count = _wht_projection(
        db, date_from=date_from, date_to=date_to
    )
    return {
        "report_basis": (
            "output_tax_invoiced_less_credit_note_adjustments_and_wht_receivable"
        ),
        "date_from": date_from or "",
        "date_to": date_to or "",
        "invoice_rows": invoice_rows,
        "output_tax_totals": output_totals,
        "output_tax_invoice_count": invoice_count,
        "output_tax_rows_truncated": invoice_count > len(invoice_rows),
        "credit_note_rows": credit_rows,
        "credit_note_tax_totals": credit_totals,
        "credit_note_count": credit_count,
        "credit_note_rows_truncated": credit_count > len(credit_rows),
        "net_output_tax_totals": _net_output_tax_totals(output_totals, credit_totals),
        "wht_rows": wht_rows,
        "wht_totals": wht_totals,
        "wht_record_count": wht_count,
        "wht_rows_truncated": wht_count > len(wht_rows),
    }


def initialize_withholding_tax_lifecycle(
    db: Session,
    record_id: str | uuid.UUID,
    *,
    actor_id: str | None,
) -> WithholdingTaxRecord:
    """Append initial pending-state evidence in the source transaction."""
    normalized_id = coerce_uuid(record_id)
    record = db.get(WithholdingTaxRecord, normalized_id)
    if record is None:
        raise TaxSourceNotFound("Withholding-tax record does not exist")
    existing = db.scalar(
        select(WithholdingTaxTransition.id)
        .where(WithholdingTaxTransition.record_id == normalized_id)
        .limit(1)
    )
    if existing is None:
        db.add(
            WithholdingTaxTransition(
                record_id=record.id,
                from_status=None,
                to_status=record.status,
                actor_id=actor_id,
                certificate_reference=record.certificate_reference,
                notes="WHT receivable recognized",
                occurred_at=record.created_at or datetime.now(UTC),
            )
        )
        db.flush()
    return record


def transition_withholding_tax(
    db: Session,
    record_id: str | uuid.UUID,
    *,
    target_status: WithholdingTaxStatus,
    actor_id: str | None,
    certificate_reference: str | None = None,
    notes: str | None = None,
    commit: bool = True,
) -> WithholdingTaxRecord:
    """Apply one legal WHT transition and append its official evidence."""
    normalized_id = coerce_uuid(record_id)
    record = db.scalar(
        select(WithholdingTaxRecord)
        .where(WithholdingTaxRecord.id == normalized_id)
        .with_for_update()
    )
    if record is None:
        raise TaxSourceNotFound("Withholding-tax record does not exist")
    initialize_withholding_tax_lifecycle(db, record.id, actor_id=None)
    previous = record.status
    if previous == target_status:
        return record
    allowed = {
        WithholdingTaxStatus.pending: {
            WithholdingTaxStatus.certified,
            WithholdingTaxStatus.written_off,
        },
        WithholdingTaxStatus.certified: {
            WithholdingTaxStatus.reclaimed,
            WithholdingTaxStatus.written_off,
        },
        WithholdingTaxStatus.reclaimed: set(),
        WithholdingTaxStatus.written_off: set(),
    }
    if target_status not in allowed[previous]:
        raise TaxAccountingError(
            f"Illegal WHT transition: {previous.value} -> {target_status.value}"
        )
    reference = certificate_reference.strip() if certificate_reference else None
    normalized_notes = notes.strip() if notes else None
    if target_status == WithholdingTaxStatus.certified and not (
        reference or record.certificate_reference or record.certificate_path
    ):
        raise TaxAccountingError(
            "A certificate reference or stored certificate is required"
        )
    if target_status == WithholdingTaxStatus.reclaimed and previous != (
        WithholdingTaxStatus.certified
    ):
        raise TaxAccountingError("WHT must be certified before it is reclaimed")
    if target_status == WithholdingTaxStatus.written_off and not normalized_notes:
        raise TaxAccountingError("A write-off reason is required")

    now = datetime.now(UTC)
    if reference:
        record.certificate_reference = reference
    if target_status == WithholdingTaxStatus.certified:
        record.certified_at = now
    if target_status in {
        WithholdingTaxStatus.reclaimed,
        WithholdingTaxStatus.written_off,
    }:
        record.resolved_at = now
    if normalized_notes:
        record.notes = (
            normalized_notes
            if not record.notes
            else f"{record.notes.rstrip()}\n{normalized_notes}"
        )
    record.status = target_status
    record.updated_at = now
    if record.payment_id is not None:
        payment = db.get(Payment, record.payment_id)
        if payment is not None:
            payment.updated_at = now
    db.add(
        WithholdingTaxTransition(
            record_id=record.id,
            from_status=previous,
            to_status=target_status,
            actor_id=actor_id,
            certificate_reference=record.certificate_reference,
            notes=normalized_notes,
            occurred_at=now,
        )
    )
    db.flush()
    if commit:
        db.commit()
        db.refresh(record)
    return record


def build_tax_operations_state(
    db: Session,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    wht_status: WithholdingTaxStatus | None = None,
    wht_search: str | None = None,
    wht_page: int = 1,
) -> dict[str, object]:
    """Build the bounded WHT operator queue; accounting remains in ERP."""
    if wht_page < 1:
        raise TaxAccountingError("WHT page must be at least 1")
    normalized_search = (wht_search or "").strip()
    filters = []
    if wht_status is not None:
        filters.append(WithholdingTaxRecord.status == wht_status)
    if normalized_search:
        pattern = f"%{normalized_search}%"
        filters.append(
            or_(
                cast(WithholdingTaxRecord.id, String).ilike(pattern),
                cast(WithholdingTaxRecord.billing_account_id, String).ilike(pattern),
                WithholdingTaxRecord.certificate_reference.ilike(pattern),
                Reseller.name.ilike(pattern),
            )
        )
    total = int(
        db.scalar(
            select(func.count(WithholdingTaxRecord.id))
            .select_from(WithholdingTaxRecord)
            .outerjoin(Reseller, Reseller.id == WithholdingTaxRecord.reseller_id)
            .where(*filters)
        )
        or 0
    )
    records = list(
        db.scalars(
            select(WithholdingTaxRecord)
            .outerjoin(Reseller, Reseller.id == WithholdingTaxRecord.reseller_id)
            .options(selectinload(WithholdingTaxRecord.reseller))
            .where(*filters)
            .order_by(
                WithholdingTaxRecord.created_at.desc(),
                WithholdingTaxRecord.id.desc(),
            )
            .offset((wht_page - 1) * TAX_OPERATIONS_PAGE_SIZE)
            .limit(TAX_OPERATIONS_PAGE_SIZE)
        ).all()
    )
    page_count = max(
        1, (total + TAX_OPERATIONS_PAGE_SIZE - 1) // TAX_OPERATIONS_PAGE_SIZE
    )
    return {
        "accounting_owner": "dotmac_erp",
        "wht_records": records,
        "wht_status_presentations": {
            str(record.id): withholding_tax_status_presentation(record.status)
            for record in records
        },
        "wht_statuses": tuple(WithholdingTaxStatus),
        "wht_filter_status": wht_status.value if wht_status is not None else "",
        "wht_search": normalized_search,
        "wht_pagination": {
            "page": wht_page,
            "page_size": TAX_OPERATIONS_PAGE_SIZE,
            "total": total,
            "page_count": page_count,
            "has_previous": wht_page > 1,
            "has_next": wht_page * TAX_OPERATIONS_PAGE_SIZE < total,
        },
        "date_from": date_from or "",
        "date_to": date_to or "",
    }
