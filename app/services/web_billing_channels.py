"""Service helpers for admin billing payment-channel routes."""

from __future__ import annotations

from app.models.billing import PaymentChannelType
from app.services import billing as billing_service


def list_payment_channels_data(db) -> dict[str, object]:
    channels = billing_service.payment_channels.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    providers = billing_service.payment_providers.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    collection_accounts = billing_service.collection_accounts.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    return {
        "channels": channels,
        "providers": providers,
        "collection_accounts": collection_accounts,
        "channel_types": [item.value for item in PaymentChannelType],
    }


def load_payment_channel_edit_data(db, channel_id: str) -> dict[str, object] | None:
    channel = billing_service.payment_channels.get(db, channel_id)
    if not channel:
        return None
    providers = billing_service.payment_providers.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    collection_accounts = billing_service.collection_accounts.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    return {
        "channel": channel,
        "providers": providers,
        "collection_accounts": collection_accounts,
        "channel_types": [item.value for item in PaymentChannelType],
    }


def list_payment_channel_accounts_data(db) -> dict[str, object]:
    mappings = billing_service.payment_channel_accounts.list(
        db=db,
        channel_id=None,
        collection_account_id=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    channels = billing_service.payment_channels.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    collection_accounts = billing_service.collection_accounts.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    return {
        "mappings": mappings,
        "channels": channels,
        "collection_accounts": collection_accounts,
    }


def load_payment_channel_account_edit_data(db, mapping_id: str) -> dict[str, object] | None:
    mapping = billing_service.payment_channel_accounts.get(db, mapping_id)
    if not mapping:
        return None
    channels = billing_service.payment_channels.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    collection_accounts = billing_service.collection_accounts.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    return {
        "mapping": mapping,
        "channels": channels,
        "collection_accounts": collection_accounts,
    }
