"""Web context builders for consolidated reseller billing UI.

Pairs with ``app/web/admin/billing_consolidated.py`` and the Jinja templates
under ``templates/admin/billing/consolidated/``.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.subscriber import Reseller
from app.services import billing as billing_service
from app.services.common import round_money, to_decimal


def build_list_context(
    db: Session,
    *,
    reseller_id: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> dict:
    offset = max(0, (page - 1) * per_page)
    items = billing_service.billing_accounts.list(
        db,
        reseller_id=reseller_id,
        is_active=True,
        limit=per_page,
        offset=offset,
        order_by="created_at",
        order_dir="desc",
    )

    reseller_ids = [item.reseller_id for item in items]
    reseller_map = {}
    if reseller_ids:
        reseller_rows = db.execute(
            select(Reseller.id, Reseller.name, Reseller.is_house).where(
                Reseller.id.in_(reseller_ids)
            )
        ).all()
        reseller_map = {r.id: r for r in reseller_rows}

    rows: list[dict[str, object]] = []
    for ba in items:
        reseller_row = reseller_map.get(ba.reseller_id)
        rows.append(
            {
                "id": str(ba.id),
                "name": ba.name,
                "reseller_id": str(ba.reseller_id),
                "reseller_name": reseller_row.name if reseller_row else "—",
                "is_house": bool(reseller_row.is_house) if reseller_row else False,
                "currency": ba.currency,
                "status": ba.status,
                "balance": round_money(to_decimal(ba.balance)),
            }
        )

    resellers = db.execute(
        select(Reseller.id, Reseller.name, Reseller.is_house).order_by(Reseller.name)
    ).all()
    return {
        "rows": rows,
        "page": page,
        "per_page": per_page,
        "selected_reseller_id": reseller_id,
        "resellers": [
            {
                "id": str(r.id),
                "name": r.name + (" (House)" if r.is_house else ""),
            }
            for r in resellers
        ],
        "active_page": "billing_consolidated",
        "active_menu": "billing",
    }


def build_detail_context(db: Session, billing_account_id: str) -> dict:
    statement = billing_service.billing_accounts.statement(db, billing_account_id)
    ba = billing_service.billing_accounts.get(db, billing_account_id)
    reseller = db.get(Reseller, ba.reseller_id)
    return {
        "billing_account": statement.billing_account,
        "subscribers": statement.subscribers,
        "recent_payments": statement.recent_payments,
        "total_outstanding": statement.total_outstanding,
        "unallocated_balance": statement.unallocated_balance,
        "reseller_name": reseller.name if reseller else "",
        "is_house": bool(reseller.is_house) if reseller else False,
        "active_page": "billing_consolidated",
        "active_menu": "billing",
    }


def record_bulk_payment(
    db: Session,
    *,
    billing_account_id: str,
    amount: str,
    currency: str = "NGN",
    memo: str | None = None,
    collection_account_id: str | None = None,
) -> UUID:
    """Record a bulk payment + auto-allocate FIFO. Returns the Payment.id."""
    from app.schemas.billing import PaymentCreate

    payload = PaymentCreate(
        billing_account_id=UUID(billing_account_id),
        amount=Decimal(amount),
        currency=currency,
        memo=memo,
        collection_account_id=UUID(collection_account_id)
        if collection_account_id
        else None,
        allocations=None,
    )
    payment = billing_service.payments.create(db, payload)
    return payment.id
