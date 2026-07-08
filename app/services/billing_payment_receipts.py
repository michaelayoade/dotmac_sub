"""Customer-facing payment receipt rendering helpers."""

from __future__ import annotations

import html
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from app.models.billing import Payment, PaymentAllocation, PaymentStatus
from app.models.subscriber import Subscriber
from app.services import email as email_service
from app.services.billing_invoice_pdf import _ensure_weasyprint_pydyf_compat
from app.services.common import coerce_uuid
from app.services.customer_portal_context import get_allowed_account_ids

logger = logging.getLogger(__name__)


def _money(value: Any) -> Decimal:
    if value is None:
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or "0.00"))


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _receipt_number(payment: Payment) -> str:
    raw = str(payment.receipt_number or "").strip()
    if raw:
        return raw if raw.startswith("#") else f"#{raw}"
    compact = str(payment.id).replace("-", "")[:8].upper()
    return f"#RCP-{compact}"


def _safe_receipt_number(payment: Payment) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", _receipt_number(payment).lstrip("#")).strip(
        "-"
    )


def _account_display(account: Subscriber | None) -> str:
    if not account:
        return "Customer"
    for value in (
        getattr(account, "name", None),
        getattr(account, "business_name", None),
        getattr(account, "username", None),
        " ".join(
            part
            for part in (
                str(getattr(account, "first_name", "") or "").strip(),
                str(getattr(account, "last_name", "") or "").strip(),
            )
            if part
        ),
    ):
        candidate = str(value or "").strip()
        if candidate:
            return candidate
    return "Customer"


def _account_email(account: Subscriber | None) -> str:
    if not account:
        return ""
    return str(
        getattr(account, "email", None) or getattr(account, "billing_email", None) or ""
    ).strip()


def _payment_method(payment: Payment) -> str:
    if payment.payment_channel and payment.payment_channel.name:
        return payment.payment_channel.name
    if payment.payment_method and payment.payment_method.method_type:
        return _enum_value(payment.payment_method.method_type).replace("_", " ").title()
    if payment.provider and payment.provider.name:
        return payment.provider.name
    return "Payment"


def get_payment_receipt_context(
    db: Session,
    payment_id: str,
    *,
    allowed_account_ids: list[str] | set[str] | None = None,
) -> dict[str, Any]:
    payment = (
        db.query(Payment)
        .options(
            selectinload(Payment.account),
            selectinload(Payment.payment_channel),
            selectinload(Payment.payment_method),
            selectinload(Payment.provider),
            selectinload(Payment.allocations).selectinload(PaymentAllocation.invoice),
        )
        .filter(Payment.id == coerce_uuid(payment_id))
        .filter(Payment.is_active.is_(True))
        .first()
    )
    if not payment or payment.status != PaymentStatus.succeeded:
        raise HTTPException(status_code=404, detail="Receipt not found")
    if payment.account_id is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    if (
        allowed_account_ids is not None
        and str(payment.account_id) not in allowed_account_ids
    ):
        raise HTTPException(status_code=404, detail="Receipt not found")

    allocations = [item for item in payment.allocations if item.is_active]
    amount_applied = sum((_money(item.amount) for item in allocations), Decimal("0.00"))
    amount_received = _money(payment.amount)
    unallocated_credit = max(Decimal("0.00"), amount_received - amount_applied)
    occurred_at = payment.paid_at or payment.created_at or datetime.now(UTC)
    account = payment.account

    allocation_rows = []
    for allocation in allocations:
        invoice = allocation.invoice
        invoice_status = (
            _enum_value(getattr(invoice, "status", "")).replace("_", " ").title()
        )
        allocation_rows.append(
            SimpleNamespace(
                invoice_id=str(allocation.invoice_id),
                invoice_number=(invoice.invoice_number if invoice else None)
                or f"INV-{str(allocation.invoice_id)[:8].upper()}",
                status=invoice_status or "Applied",
                amount=_money(allocation.amount),
            )
        )

    return {
        "payment": payment,
        "receipt_number": _receipt_number(payment),
        "receipt_date": occurred_at,
        "account_number": getattr(account, "account_number", None)
        or getattr(account, "splynx_customer_id", None)
        or str(payment.account_id)[:8],
        "received_from": _account_display(account),
        "received_email": _account_email(account),
        "amount_received": amount_received,
        "amount_applied": amount_applied,
        "unallocated_credit": unallocated_credit,
        "currency": payment.currency or "NGN",
        "status": _enum_value(payment.status).replace("_", " ").title(),
        "transaction_ref": payment.external_id
        or payment.receipt_number
        or str(payment.id),
        "method": _payment_method(payment),
        "allocations": allocation_rows,
    }


def get_customer_payment_receipt_context(
    db: Session, customer: dict, payment_id: str
) -> dict[str, Any]:
    allowed = get_allowed_account_ids(customer, db)
    return get_payment_receipt_context(db, payment_id, allowed_account_ids=allowed)


def _format_amount(currency: str, amount: Decimal) -> str:
    symbol = "₦" if currency == "NGN" else f"{currency} "
    return f"{symbol}{amount:,.2f}"


def render_receipt_document_html(context: dict[str, Any]) -> str:
    rows = []
    for allocation in context["allocations"]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(allocation.invoice_number)}</td>"
            f"<td>{html.escape(allocation.status)}</td>"
            f"<td class='right'>{html.escape(_format_amount(context['currency'], allocation.amount))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(
            "<tr><td colspan='3' class='muted empty'>No invoice allocation recorded.</td></tr>"
        )

    date_value = context["receipt_date"].strftime("%Y-%m-%d")
    payment_date = context["receipt_date"].strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{ size: A4; margin: 14mm; }}
body {{ margin: 0; background: #fff; color: #111827; font-family: Inter, Arial, sans-serif; }}
.receipt {{ border: 1px solid #e5e7eb; border-radius: 14px; overflow: hidden; }}
.header {{ background: #176f37; color: white; padding: 24px 28px 18px; min-height: 116px; position: relative; }}
.logo {{ display: inline-block; background: white; color: #176f37; font-weight: 900; font-size: 25px; letter-spacing: .5px; padding: 8px 14px; }}
.company {{ position: absolute; left: 28px; bottom: 16px; font-size: 17px; font-weight: 800; }}
.receipt-no {{ position: absolute; right: 24px; top: 22px; width: 190px; border-radius: 12px; background: white; color: #111827; padding: 16px; }}
.receipt-no .label {{ color: #176f37; font-size: 11px; font-weight: 800; }}
.receipt-no .number {{ margin-top: 8px; font-size: 18px; font-weight: 900; }}
.receipt-no .meta {{ margin-top: 8px; font-size: 11px; line-height: 1.7; }}
.body {{ padding: 22px 22px 28px; }}
.cards {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }}
.card {{ border: 1px solid #e5e7eb; border-radius: 8px; padding: 13px; min-height: 58px; }}
.card.highlight {{ border-color: #7fd0a0; background: #edfff5; }}
.kicker {{ color: #176f37; font-size: 11px; font-weight: 900; }}
.value {{ margin-top: 10px; font-size: 13px; font-weight: 700; }}
.big {{ color: #176f37; font-size: 20px; font-weight: 900; }}
.sub {{ margin-top: 7px; color: #6b7280; font-size: 11px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 30px; border: 1px solid #e5e7eb; border-radius: 8px; overflow: hidden; }}
thead {{ background: #176f37; color: white; }}
th {{ padding: 13px 14px; font-size: 11px; text-align: left; }}
td {{ padding: 14px; font-size: 13px; height: 38px; border-top: 1px solid #f3f4f6; }}
.right {{ text-align: right; }}
.summary {{ margin: 30px 0 30px auto; width: 205px; border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px 14px; }}
.summary-row {{ display: flex; justify-content: space-between; gap: 12px; font-size: 12px; line-height: 2.1; }}
.summary-row strong {{ font-weight: 900; }}
.green {{ color: #176f37; }}
.details {{ border-left: 6px solid #22c55e; border-radius: 0 8px 8px 0; background: #edfff5; padding: 14px 16px; font-size: 12px; }}
.details-title {{ color: #176f37; font-weight: 900; margin-bottom: 8px; }}
.footer {{ display: flex; justify-content: space-between; margin-top: 62px; color: #6b7280; font-size: 11px; }}
.muted {{ color: #6b7280; }}
.empty {{ text-align: center; padding: 34px; }}
</style>
</head>
<body>
<div class="receipt">
  <div class="header">
    <div class="logo">DOTMAC</div>
    <div class="company">{html.escape(context["received_from"])}</div>
    <div class="receipt-no">
      <div class="label">RECEIPT</div>
      <div class="number">{html.escape(context["receipt_number"])}</div>
      <div class="meta">Date: {html.escape(date_value)}<br>Account: {html.escape(str(context["account_number"]))}</div>
    </div>
  </div>
  <div class="body">
    <div class="cards">
      <div class="card"><div class="kicker">RECEIVED FROM</div><div class="value">{html.escape(context["received_from"])}</div><div class="sub">{html.escape(context["received_email"])}</div></div>
      <div class="card highlight"><div class="kicker">AMOUNT RECEIVED</div><div class="big">{html.escape(_format_amount(context["currency"], context["amount_received"]))}</div><div class="sub">Status: {html.escape(context["status"])}</div></div>
      <div class="card"><div class="kicker">TRANSACTION REF</div><div class="value">{html.escape(context["transaction_ref"])}</div><div class="sub">Method: {html.escape(context["method"])}</div></div>
    </div>
    <table>
      <thead><tr><th>INVOICE</th><th>STATUS</th><th class="right">AMOUNT APPLIED</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    <div class="summary">
      <div class="summary-row"><span>Amount Received</span><strong>{html.escape(_format_amount(context["currency"], context["amount_received"]))}</strong></div>
      <div class="summary-row"><span>Amount Applied</span><strong>{html.escape(_format_amount(context["currency"], context["amount_applied"]))}</strong></div>
      <div class="summary-row green"><span>Unallocated Credit</span><strong>{html.escape(_format_amount(context["currency"], context["unallocated_credit"]))}</strong></div>
    </div>
    <div class="details">
      <div class="details-title">PAYMENT DETAILS</div>
      <div>Payment Date: {html.escape(payment_date)}</div>
      <div>Payment ID: {html.escape(str(context["payment"].id))}</div>
    </div>
    <div class="footer"><span>Prepared by Dotmac Selfcare</span><span>Thank you for your business.</span></div>
  </div>
</div>
</body>
</html>"""


def build_receipt_pdf(context: dict[str, Any]) -> bytes:
    _ensure_weasyprint_pydyf_compat()
    from weasyprint import HTML

    return HTML(string=render_receipt_document_html(context)).write_pdf()


def download_filename(payment: Payment) -> str:
    return f"receipt-{_safe_receipt_number(payment)}.pdf"


def send_receipt_email(
    db: Session,
    payment_id: str,
    to_email: str,
    *,
    base_url: str = "https://selfcare.dotmac.ng",
) -> bool:
    context = get_payment_receipt_context(db, payment_id, allowed_account_ids=None)
    receipt_url = f"{base_url.rstrip('/')}/portal/billing/payments/{context['payment'].id}/receipt"
    subject = f"Payment receipt {context['receipt_number']}"
    body_html = (
        f"<p>Payment receipt {html.escape(context['receipt_number'])}</p>"
        f"<p>Amount received: <strong>{html.escape(_format_amount(context['currency'], context['amount_received']))}</strong></p>"
        f'<p>View or download the receipt here: <a href="{html.escape(receipt_url)}">{html.escape(receipt_url)}</a></p>'
    )
    body_text = (
        f"Payment receipt {context['receipt_number']}\n"
        f"Amount received: {_format_amount(context['currency'], context['amount_received'])}\n"
        f"View or download: {receipt_url}"
    )
    return email_service.send_email(
        db,
        to_email,
        subject,
        body_html,
        body_text=body_text,
        activity="billing_payment_receipt",
    )
