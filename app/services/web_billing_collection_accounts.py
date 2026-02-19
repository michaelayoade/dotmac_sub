"""Service helpers for billing collection-account web routes."""

from __future__ import annotations

from app.models.billing import CollectionAccountType
from app.services import billing as billing_service


def list_data(db, *, show_inactive: bool) -> dict[str, object]:
    accounts = billing_service.collection_accounts.list(
        db=db,
        is_active=False if show_inactive else None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    return {
        "accounts": accounts,
        "account_types": [item.value for item in CollectionAccountType],
        "show_inactive": show_inactive,
    }


def edit_data(db, *, account_id: str) -> dict[str, object] | None:
    account = billing_service.collection_accounts.get(db, account_id)
    if not account:
        return None
    return {
        "account": account,
        "account_types": [item.value for item in CollectionAccountType],
    }
