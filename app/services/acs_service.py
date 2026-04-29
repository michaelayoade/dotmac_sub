"""Application-facing ACS facade.

This module is the preferred boundary for application code that needs ACS
reads, writes, or webhook ingestion. The lower-level adapters stay split by
responsibility because reads, writes, and informs have different failure modes,
but callers should not need to assemble those ports directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.services.acs_client import (
    AcsConfigWriter,
    AcsEventIngestor,
    AcsStateReader,
    create_acs_config_writer,
    create_acs_event_ingestor,
    create_acs_state_reader,
)


@dataclass
class AcsService:
    """Facade over the configured ACS backend ports."""

    kind: str | None = None
    _config_writer: AcsConfigWriter | None = None
    _state_reader: AcsStateReader | None = None
    _event_ingestor: AcsEventIngestor | None = None

    @property
    def config_writer(self) -> AcsConfigWriter:
        if self._config_writer is None:
            self._config_writer = create_acs_config_writer(self.kind)
        return self._config_writer

    @property
    def state_reader(self) -> AcsStateReader:
        if self._state_reader is None:
            self._state_reader = create_acs_state_reader(self.kind)
        return self._state_reader

    @property
    def event_ingestor(self) -> AcsEventIngestor:
        if self._event_ingestor is None:
            self._event_ingestor = create_acs_event_ingestor(self.kind)
        return self._event_ingestor

    def execute_config_action(
        self,
        db: Session,
        action: str,
        ont_id: str,
        *,
        args: list[object] | tuple[object, ...] | None = None,
        kwargs: dict[str, object] | None = None,
    ) -> Any:
        return self.config_writer.execute_config_action(
            db,
            action,
            ont_id,
            args=args,
            kwargs=kwargs,
        )

    def send_connection_request(self, db: Session, ont_id: str) -> Any:
        return self.config_writer.send_connection_request(db, ont_id)

    def get_device_summary(
        self,
        db: Session,
        ont_id: str,
        *,
        persist_observed_runtime: bool = False,
    ) -> Any:
        return self.state_reader.get_device_summary(
            db,
            ont_id,
            persist_observed_runtime=persist_observed_runtime,
        )

    def get_lan_hosts(self, db: Session, ont_id: str) -> list[dict[str, Any]]:
        return self.state_reader.get_lan_hosts(db, ont_id)

    def get_ethernet_ports(self, db: Session, ont_id: str) -> list[dict[str, Any]]:
        return self.state_reader.get_ethernet_ports(db, ont_id)

    def persist_observed_runtime(
        self,
        db: Session,
        ont: object,
        summary: object,
        *,
        commit: bool = True,
    ) -> None:
        self.state_reader.persist_observed_runtime(
            db,
            ont,
            summary,
            commit=commit,
        )

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
        return self.event_ingestor.receive_inform(
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


def create_acs_service(kind: str | None = None) -> AcsService:
    """Create the application ACS facade for the configured backend."""
    return AcsService(kind=kind)
