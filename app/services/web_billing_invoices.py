"""Service helpers for admin billing invoice routes."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import TypedDict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import CreditNote, CreditNoteStatus, InvoiceStatus
from app.models.subscriber import Subscriber
from app.schemas.billing import InvoiceLineCreate, InvoiceLineUpdate, InvoiceUpdate
from app.services import audit as audit_service
from app.services import billing as billing_service
from app.services.audit_helpers import extract_changes, format_changes

logger = logging.getLogger(__name__)

class InvoiceLineItem(TypedDict):
    description: str
    quantity: Decimal
    unit_price: Decimal
    tax_rate_id: UUID | None


def build_invoice_payload_data(
    *,
    account_id,
    invoice_number: str | None,
    status: str | None,
    currency: str,
    issued_at,
    due_at,
    memo: str | None,
) -> dict[str, object]:
    """Build invoice create/update payload dictionary."""
    return {
        "account_id": account_id,
        "invoice_number": invoice_number.strip() if invoice_number else None,
        "status": status or "draft",
        "currency": currency.strip().upper(),
        "issued_at": issued_at,
        "due_at": due_at,
        "memo": memo.strip() if memo else None,
    }


def parse_create_line_items(
    *,
    line_items_json: str | None,
    line_description: list[str],
    line_quantity: list[str],
    line_unit_price: list[str],
    line_tax_rate_id: list[str],
    parse_decimal,
) -> list[InvoiceLineItem]:
    """Parse line items from JSON or legacy array fields."""
    line_items: list[InvoiceLineItem] = []
    if line_items_json and line_items_json.strip():
        try:
            items_data = json.loads(line_items_json)
            if not isinstance(items_data, list):
                items_data = []
            for item in items_data:
                if not isinstance(item, dict):
                    continue
                description = str(item.get("description", "")).strip()
                if not description:
                    continue
                line_items.append(
                    {
                        "description": description,
                        "quantity": Decimal(str(item.get("quantity", 1))),
                        "unit_price": Decimal(str(item.get("unitPrice", 0))),
                        "tax_rate_id": UUID(str(item["taxRateId"]))
                        if item.get("taxRateId")
                        else None,
                    }
                )
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
    if line_items:
        return line_items

    for idx, description in enumerate(line_description):
        if not description or not description.strip():
            continue
        quantity_raw = line_quantity[idx] if idx < len(line_quantity) else ""
        unit_price_raw = line_unit_price[idx] if idx < len(line_unit_price) else ""
        tax_rate_raw = line_tax_rate_id[idx] if idx < len(line_tax_rate_id) else ""
        line_items.append(
            {
                "description": description.strip(),
                "quantity": parse_decimal(quantity_raw, "quantity", Decimal("1")),
                "unit_price": parse_decimal(unit_price_raw, "unit_price", Decimal("0.00")),
                "tax_rate_id": UUID(tax_rate_raw) if tax_rate_raw else None,
            }
        )
    return line_items


def create_invoice_lines(db: Session, *, invoice_id: UUID, line_items: list[InvoiceLineItem]) -> None:
    """Create invoice lines from parsed line item dictionaries."""
    for item in line_items:
        billing_service.invoice_lines.create(
            db,
            InvoiceLineCreate(
                invoice_id=invoice_id,
                description=item["description"],
                quantity=item["quantity"],
                unit_price=item["unit_price"],
                tax_rate_id=item["tax_rate_id"],
            ),
        )


def maybe_issue_invoice(db: Session, *, invoice_id, issue_immediately: str | None):
    """Issue invoice when requested."""
    if not issue_immediately:
        return None
    billing_service.invoices.update(
        db=db,
        invoice_id=str(invoice_id),
        payload=InvoiceUpdate(status=InvoiceStatus.issued, issued_at=datetime.now(UTC)),
    )
    return billing_service.invoices.get(db=db, invoice_id=str(invoice_id))


def maybe_send_invoice_notification(db: Session, *, invoice, send_notification: str | None) -> None:
    """Send invoice email notification when requested."""
    if not send_notification or not invoice or not invoice.account:
        return
    from app.services import email as email_service

    account = invoice.account
    email_addr = getattr(account, "email", None)
    if not email_addr:
        return
    inv_num = invoice.invoice_number or str(invoice.id)
    total = getattr(invoice, "total", "0.00")
    currency = getattr(invoice, "currency", "")
    body_html = (
        f"<p>Dear Customer,</p>"
        f"<p>Invoice <strong>{inv_num}</strong> has been issued"
        f" for {currency} {total}.</p>"
        f"<p>Please review and arrange payment at your earliest convenience.</p>"
    )
    email_service.send_email(
        db=db,
        to_email=email_addr,
        subject=f"Invoice {inv_num}",
        body_html=body_html,
    )


def apply_line_items_json_update(db: Session, *, invoice_id, before_invoice, line_items_json: str | None) -> None:
    """Apply line item create/update/delete mutations from JSON payload."""
    if not line_items_json or not line_items_json.strip():
        return
    try:
        items_data = json.loads(line_items_json)
    except json.JSONDecodeError:
        return
    if items_data is None:
        return

    existing_lines = {
        str(line.id): line
        for line in (before_invoice.lines or [])
        if getattr(line, "is_active", True)
    }
    seen_ids: set[str] = set()
    for item in items_data:
        description = str(item.get("description", "")).strip()
        if not description:
            continue
        quantity = Decimal(str(item.get("quantity", 1)))
        unit_price = Decimal(str(item.get("unitPrice", 0)))
        tax_rate_id = item.get("taxRateId") or item.get("tax_rate_id")
        line_id = item.get("id") or item.get("lineId") or item.get("line_id")
        if line_id and str(line_id) in existing_lines:
            seen_ids.add(str(line_id))
            billing_service.invoice_lines.update(
                db,
                str(line_id),
                InvoiceLineUpdate(
                    description=description,
                    quantity=quantity,
                    unit_price=unit_price,
                    tax_rate_id=UUID(tax_rate_id) if tax_rate_id else None,
                ),
            )
        else:
            billing_service.invoice_lines.create(
                db,
                InvoiceLineCreate(
                    invoice_id=invoice_id,
                    description=description,
                    quantity=quantity,
                    unit_price=unit_price,
                    tax_rate_id=UUID(tax_rate_id) if tax_rate_id else None,
                ),
            )
    for line_id in existing_lines:
        if line_id not in seen_ids:
            billing_service.invoice_lines.delete(db, line_id)


def load_tax_rates(db: Session):
    return billing_service.tax_rates.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )


def load_credit_notes_for_account(db: Session, *, account_id):
    stmt = (
        select(CreditNote)
        .where(CreditNote.account_id == account_id)
        .where(CreditNote.is_active.is_(True))
        .where(
            CreditNote.status.in_([CreditNoteStatus.issued, CreditNoteStatus.partially_applied])
        )
        .order_by(CreditNote.created_at.desc())
    )
    return db.scalars(stmt).all()


def build_invoice_activities(db: Session, *, invoice_id: str) -> list[dict]:
    audit_events = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type="invoice",
        entity_id=invoice_id,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    actor_ids = {str(event.actor_id) for event in audit_events if getattr(event, "actor_id", None)}
    people = {}
    if actor_ids:
        people = {
            str(person.id): person
            for person in db.scalars(select(Subscriber).where(Subscriber.id.in_(actor_ids))).all()
        }
    activities = []
    for event in audit_events:
        actor = people.get(str(event.actor_id)) if getattr(event, "actor_id", None) else None
        actor_name = f"{actor.first_name} {actor.last_name}".strip() if actor else "System"
        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes, max_items=2)
        activities.append(
            {
                "title": (event.action or "Activity").replace("_", " ").title(),
                "description": f"{actor_name}" + (f" · {change_summary}" if change_summary else ""),
                "occurred_at": event.occurred_at,
            }
        )
    return activities


def load_invoice_detail_data(db: Session, *, invoice_id: str) -> dict[str, object] | None:
    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if not invoice:
        return None
    return {
        "invoice": invoice,
        "tax_rates": load_tax_rates(db),
        "credit_notes": load_credit_notes_for_account(db, account_id=invoice.account_id),
        "activities": build_invoice_activities(db, invoice_id=invoice_id),
    }
