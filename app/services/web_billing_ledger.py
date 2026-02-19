"""Service helpers for billing ledger web routes."""

from __future__ import annotations

from uuid import UUID

from app.models.billing import LedgerEntry, LedgerEntryType
from app.services import billing as billing_service
from app.services import web_billing_customers as web_billing_customers_service
from app.services.common import validate_enum


def build_ledger_entries_data(
    db,
    *,
    customer_ref: str | None,
    entry_type: str | None,
) -> dict[str, object]:
    account_ids = []
    if customer_ref:
        account_ids = [
            UUID(item["id"])
            for item in web_billing_customers_service.accounts_for_customer(db, customer_ref)
        ]

    entries = []
    if account_ids:
        query = db.query(LedgerEntry).filter(LedgerEntry.account_id.in_(account_ids))
        if entry_type:
            query = query.filter(
                LedgerEntry.entry_type == validate_enum(entry_type, LedgerEntryType, "entry_type")
            )
        query = query.filter(LedgerEntry.is_active.is_(True))
        entries = query.order_by(LedgerEntry.created_at.desc()).limit(200).offset(0).all()
    elif not customer_ref:
        entries = billing_service.ledger_entries.list(
            db=db,
            account_id=None,
            entry_type=entry_type,
            source=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=200,
            offset=0,
        )

    return {
        "entries": entries,
        "entry_type": entry_type,
        "customer_ref": customer_ref,
    }
