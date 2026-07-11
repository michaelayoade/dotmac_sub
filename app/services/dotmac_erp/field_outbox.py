from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.field_erp import FieldErpSyncEvent
from app.models.field_expense import FieldExpenseRequest
from app.models.field_material import FieldMaterialRequest
from app.services.dotmac_erp.client import (
    DotMacERPClient,
    DotMacERPError,
    dotmac_erp_client_from_settings,
)


@dataclass
class FieldErpOutboxResult:
    processed: int = 0
    synced: int = 0
    failed: int = 0
    canceled: int = 0


class DotMacERPFieldOutboxSync:
    def __init__(self, client: DotMacERPClient, db: Session) -> None:
        self.client = client
        self.db = db

    def close(self) -> None:
        self.client.close()

    def process_pending(self, *, limit: int = 50) -> FieldErpOutboxResult:
        result = FieldErpOutboxResult()
        events = (
            self.db.query(FieldErpSyncEvent)
            .filter(FieldErpSyncEvent.status.in_(("pending", "failed")))
            .order_by(FieldErpSyncEvent.created_at.asc())
            .limit(limit)
            .all()
        )
        for event in events:
            result.processed += 1
            outcome = self.process_event(event)
            if outcome == "synced":
                result.synced += 1
            elif outcome == "canceled":
                result.canceled += 1
            else:
                result.failed += 1
        return result

    def process_event(self, event: FieldErpSyncEvent) -> str:
        event.status = "processing"
        event.attempts += 1
        event.last_attempt_at = datetime.now(UTC)
        self.db.flush()

        try:
            if event.entity_type == "field_material_request":
                response = self.client.push_material_request(
                    event.payload,
                    idempotency_key=event.idempotency_key,
                )
                self._apply_material_response(event, response)
            elif event.entity_type == "field_expense_request":
                response = self.client.push_expense_claim(
                    event.payload,
                    idempotency_key=event.idempotency_key,
                )
                self._apply_expense_response(event, response)
            else:
                event.status = "canceled"
                event.last_error = f"Unsupported ERP outbox entity: {event.entity_type}"
                self.db.commit()
                return "canceled"
        except DotMacERPError as exc:
            event.status = "failed"
            event.last_error = str(exc)[:2000]
            self.db.commit()
            return "failed"

        event.status = "synced"
        event.last_error = None
        event.synced_at = datetime.now(UTC)
        self.db.commit()
        return "synced"

    def _apply_material_response(
        self,
        event: FieldErpSyncEvent,
        response: dict[str, Any],
    ) -> None:
        remote_id = _first_string(
            response,
            "request_id",
            "material_request_id",
            "request_number",
        )
        remote_status = _status_string(
            response,
            "material_status",
            "erp_material_status",
            "status",
        )
        event.remote_id = remote_id
        event.remote_number = _first_string(response, "request_number") or remote_id
        event.remote_status = remote_status

        request = self.db.get(FieldMaterialRequest, event.entity_id)
        if request is not None:
            if remote_id and not request.erp_material_request_id:
                request.erp_material_request_id = remote_id
            if remote_status:
                request.erp_material_status = remote_status

    def _apply_expense_response(
        self,
        event: FieldErpSyncEvent,
        response: dict[str, Any],
    ) -> None:
        remote_id = _first_string(
            response, "claim_id", "expense_claim_id", "claim_number"
        )
        remote_number = _first_string(response, "claim_number") or remote_id
        remote_status = _status_string(response, "claim_status", "status")
        event.remote_id = remote_id
        event.remote_number = remote_number
        event.remote_status = remote_status

        request = self.db.get(FieldExpenseRequest, event.entity_id)
        if request is not None:
            if remote_id and not request.erp_expense_claim_id:
                request.erp_expense_claim_id = remote_id
            if remote_number:
                request.erp_claim_number = remote_number[:60]
            if remote_status:
                request.erp_claim_status = remote_status


def _first_string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _status_string(payload: dict[str, Any], *keys: str) -> str | None:
    value = _first_string(payload, *keys)
    if not value:
        return None
    status = value.strip().lower().replace("-", "_").replace(" ", "_")
    return status[:40] if status else None


def dotmac_erp_field_outbox_sync(db: Session) -> DotMacERPFieldOutboxSync:
    return DotMacERPFieldOutboxSync(dotmac_erp_client_from_settings(db), db)
