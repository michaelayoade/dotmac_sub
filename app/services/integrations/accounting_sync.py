"""Accounting connector sync framework.

Provides a single interface for accounting providers (QuickBooks, Xero, Sage)
and dashboard helpers used by admin integrations pages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from fastapi import HTTPException
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.models.connector import ConnectorConfig
from app.services import connector as connector_service
from app.services.integrations.connectors import quickbooks, sage, xero

ACCOUNTING_PROVIDERS = ("quickbooks", "xero", "sage")


@dataclass
class SyncResult:
    provider: str
    synced_at: datetime
    status: str
    records: dict[str, int]
    details: str


class AccountingAdapter(Protocol):
    provider: str

    def sync_invoices(self, db: Session, connector: ConnectorConfig) -> int: ...

    def sync_payments(self, db: Session, connector: ConnectorConfig) -> int: ...

    def sync_customers(self, db: Session, connector: ConnectorConfig) -> int: ...

    def sync_credit_notes(self, db: Session, connector: ConnectorConfig) -> int: ...

    def get_sync_status(self, db: Session, connector: ConnectorConfig) -> dict[str, object]: ...


_ADAPTERS: dict[str, AccountingAdapter] = {
    "quickbooks": quickbooks.QuickBooksAdapter(),
    "xero": xero.XeroAdapter(),
    "sage": sage.SageAdapter(),
}


def _provider_for_connector(connector: ConnectorConfig) -> str | None:
    metadata = dict(connector.metadata_ or {})
    configured = str(metadata.get("accounting_provider") or "").strip().lower()
    if configured in ACCOUNTING_PROVIDERS:
        return configured

    name_lower = str(connector.name or "").lower()
    if "quickbooks" in name_lower:
        return "quickbooks"
    if "xero" in name_lower:
        return "xero"
    if "sage" in name_lower:
        return "sage"
    return None


def _update_connector_metadata(
    db: Session,
    connector: ConnectorConfig,
    *,
    provider: str,
    status: str,
    records: dict[str, int],
    details: str,
) -> None:
    metadata = dict(connector.metadata_ or {})
    metadata["accounting_provider"] = provider
    metadata["accounting_sync"] = {
        "last_sync_at": datetime.now(UTC).isoformat(),
        "status": status,
        "records": records,
        "details": details,
    }
    connector.metadata_ = metadata
    db.add(connector)
    db.commit()
    db.refresh(connector)


def run_sync_for_connector(db: Session, connector_id: str) -> SyncResult:
    connector = connector_service.connector_configs.get(db, connector_id)
    provider = _provider_for_connector(connector)
    if not provider:
        raise HTTPException(status_code=400, detail="Connector is not configured as an accounting provider")

    adapter = _ADAPTERS.get(provider)
    if not adapter:
        raise HTTPException(status_code=400, detail=f"No adapter registered for provider: {provider}")

    try:
        invoices = adapter.sync_invoices(db, connector)
        payments = adapter.sync_payments(db, connector)
        customers = adapter.sync_customers(db, connector)
        credit_notes = adapter.sync_credit_notes(db, connector)
    except NotImplementedError as exc:
        logger.warning("Sync not implemented for %s: %s", provider, exc)
        raise HTTPException(
            status_code=501,
            detail=f"{provider.title()} sync is not yet implemented",
        ) from exc

    records = {
        "invoices": int(invoices),
        "payments": int(payments),
        "customers": int(customers),
        "credit_notes": int(credit_notes),
    }
    details = "Sync completed"
    _update_connector_metadata(
        db,
        connector,
        provider=provider,
        status="ok",
        records=records,
        details=details,
    )
    return SyncResult(
        provider=provider,
        synced_at=datetime.now(UTC),
        status="ok",
        records=records,
        details=details,
    )


def save_field_mapping(db: Session, connector_id: str, mapping: dict[str, str]) -> ConnectorConfig:
    connector = connector_service.connector_configs.get(db, connector_id)
    provider = _provider_for_connector(connector)
    if not provider:
        raise HTTPException(status_code=400, detail="Connector is not configured as an accounting provider")
    metadata = dict(connector.metadata_ or {})
    metadata["accounting_provider"] = provider
    metadata["field_mapping"] = mapping
    connector.metadata_ = metadata
    db.add(connector)
    db.commit()
    db.refresh(connector)
    return connector


def dashboard_state(db: Session) -> dict[str, object]:
    connectors = connector_service.connector_configs.list_all(
        db,
        connector_type=None,
        auth_type=None,
        order_by="name",
        order_dir="asc",
        limit=1000,
        offset=0,
    )

    by_provider: dict[str, ConnectorConfig | None] = dict.fromkeys(ACCOUNTING_PROVIDERS)
    for connector in connectors:
        provider = _provider_for_connector(connector)
        if provider and by_provider.get(provider) is None:
            by_provider[provider] = connector

    rows: list[dict[str, object]] = []
    for provider in ACCOUNTING_PROVIDERS:
        connector = by_provider.get(provider)
        if connector is None:
            rows.append(
                {
                    "provider": provider,
                    "label": provider.title(),
                    "configured": False,
                    "connector": None,
                    "sync": {},
                    "field_mapping": {},
                }
            )
            continue

        metadata = dict(connector.metadata_ or {})
        sync_state = metadata.get("accounting_sync") if isinstance(metadata.get("accounting_sync"), dict) else {}
        field_mapping = metadata.get("field_mapping") if isinstance(metadata.get("field_mapping"), dict) else {}
        adapter = _ADAPTERS[provider]
        status = adapter.get_sync_status(db, connector)
        rows.append(
            {
                "provider": provider,
                "label": provider.title(),
                "configured": True,
                "connector": connector,
                "sync": {
                    **sync_state,
                    **status,
                },
                "field_mapping": field_mapping,
            }
        )

    return {"providers": rows}
