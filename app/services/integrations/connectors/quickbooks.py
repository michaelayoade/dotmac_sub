"""QuickBooks accounting connector adapter (not yet implemented)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.connector import ConnectorConfig

logger = logging.getLogger(__name__)


class QuickBooksAdapter:
    provider = "quickbooks"

    def sync_invoices(self, db: Session, connector: ConnectorConfig) -> int:
        raise NotImplementedError("QuickBooks invoice sync is not yet implemented")

    def sync_payments(self, db: Session, connector: ConnectorConfig) -> int:
        raise NotImplementedError("QuickBooks payment sync is not yet implemented")

    def sync_customers(self, db: Session, connector: ConnectorConfig) -> int:
        raise NotImplementedError("QuickBooks customer sync is not yet implemented")

    def sync_credit_notes(self, db: Session, connector: ConnectorConfig) -> int:
        raise NotImplementedError("QuickBooks credit note sync is not yet implemented")

    def get_sync_status(
        self, db: Session, connector: ConnectorConfig
    ) -> dict[str, object]:
        return {"provider": self.provider, "status": "not_implemented"}
