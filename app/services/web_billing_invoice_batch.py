"""Service helpers for billing invoice batch routes."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.services import billing_automation as billing_automation_service


def parse_billing_cycle(value: str | None, parse_cycle_fn):
    return parse_cycle_fn(value)


def run_batch(db, *, billing_cycle: str | None, parse_cycle_fn) -> str:
    """Run invoice cycle and return user-facing note."""
    try:
        summary = billing_automation_service.run_invoice_cycle(
            db=db,
            billing_cycle=parse_billing_cycle(billing_cycle, parse_cycle_fn),
            dry_run=False,
        )
        return f"Batch run completed. Invoices created: {summary.get('invoices_created', 0)}."
    except Exception as exc:
        return f"Batch run failed: {exc}"


def preview_batch(
    db,
    *,
    billing_cycle: str | None,
    billing_date: str | None,
    parse_cycle_fn,
) -> dict[str, object]:
    """Run dry-run invoice preview and return JSON payload."""
    run_date = None
    if billing_date:
        run_date = datetime.strptime(billing_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    summary = billing_automation_service.run_invoice_cycle(
        db=db,
        billing_cycle=parse_billing_cycle(billing_cycle, parse_cycle_fn),
        dry_run=True,
        run_at=run_date,
    )
    total_amount = summary.get("total_amount", Decimal("0.00"))
    subscriptions = summary.get("subscriptions", [])
    return {
        "invoice_count": summary.get("invoices_created", 0),
        "account_count": summary.get(
            "accounts_affected",
            len(set(s.get("account_id") for s in subscriptions)),
        ),
        "total_amount": float(total_amount),
        "total_amount_formatted": f"NGN {total_amount:,.2f}",
        "subscriptions": [
            {
                "id": str(s.get("id", "")),
                "offer_name": s.get("offer_name", "Unknown"),
                "amount": float(s.get("amount", 0)),
                "amount_formatted": f"NGN {s.get('amount', 0):,.2f}",
            }
            for s in subscriptions[:50]
        ],
    }


def preview_error_payload(exc: Exception) -> dict[str, object]:
    return {
        "error": str(exc),
        "invoice_count": 0,
        "account_count": 0,
        "total_amount_formatted": "NGN 0.00",
    }
