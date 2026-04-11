"""Service helpers for billing collection-account web routes."""

from __future__ import annotations

import logging
from uuid import UUID

from app.models.billing import CollectionAccountType
from app.schemas.billing import CollectionAccountUpdate
from app.services import billing as billing_service
from app.services.billing import configuration as billing_config_service

logger = logging.getLogger(__name__)


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


def create_collection_account_from_form(
    db,
    *,
    name: str,
    account_type: str,
    currency: str,
    bank_name: str | None,
    account_last4: str | None,
    notes: str | None,
    is_active: str | None,
):
    return billing_config_service.create_collection_account(
        db=db,
        name=name,
        account_type=account_type,
        currency=currency,
        bank_name=bank_name,
        account_last4=account_last4,
        notes=notes,
        is_active=is_active,
    )


def update_collection_account_from_form(
    db,
    *,
    account_id: UUID,
    name: str,
    account_type: str,
    currency: str,
    bank_name: str | None,
    account_last4: str | None,
    notes: str | None,
    is_active: str | None,
):
    return billing_config_service.update_collection_account(
        db=db,
        account_id=account_id,
        name=name,
        account_type=account_type,
        currency=currency,
        bank_name=bank_name,
        account_last4=account_last4,
        notes=notes,
        is_active=is_active,
    )


def deactivate_collection_account(db, *, account_id: UUID) -> None:
    billing_service.collection_accounts.delete(db, str(account_id))


def activate_collection_account(db, *, account_id: UUID):
    return billing_service.collection_accounts.update(
        db,
        str(account_id),
        CollectionAccountUpdate(is_active=True),
    )
