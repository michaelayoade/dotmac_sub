"""ACS webhook/event ingestion implementations."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session


class GenieAcsEventIngestor:
    """Ingest GenieACS webhook payloads into normalized local ACS state."""

    def receive_inform(
        self,
        db: Session,
        *,
        serial_number: str | None,
        device_id_raw: str | None,
        event: Any,
        raw_payload: dict[str, Any] | None = None,
        request_id: str | None = None,
        remote_addr: str | None = None,
        headers: dict[str, Any] | None = None,
        oui: str | None = None,
        product_class: str | None = None,
        acs_server_id: str | None = None,
    ) -> dict[str, Any]:
        from app.services import tr069 as tr069_service

        return tr069_service.receive_inform(
            db,
            serial_number=serial_number,
            device_id_raw=device_id_raw,
            event=event,
            raw_payload=raw_payload,
            request_id=request_id,
            remote_addr=remote_addr,
            headers=headers,
            oui=oui,
            product_class=product_class,
            acs_server_id=acs_server_id,
        )
