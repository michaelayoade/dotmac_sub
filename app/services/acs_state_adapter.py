"""ACS observed-state reader implementations."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session


class GenieAcsStateReader:
    """Read observed ONT state from GenieACS/TR-069 sources."""

    def get_device_summary(
        self,
        db: Session,
        ont_id: str,
        *,
        persist_observed_runtime: bool = False,
    ) -> Any:
        from app.services.network.ont_tr069 import OntTR069

        return OntTR069.get_device_summary(
            db,
            ont_id,
            persist_observed_runtime=persist_observed_runtime,
        )

    def get_lan_hosts(self, db: Session, ont_id: str) -> list[dict[str, Any]]:
        from app.services.network.ont_tr069 import OntTR069

        return OntTR069.get_lan_hosts(db, ont_id)

    def get_ethernet_ports(self, db: Session, ont_id: str) -> list[dict[str, Any]]:
        from app.services.network.ont_tr069 import OntTR069

        return OntTR069.get_ethernet_ports(db, ont_id)

    def persist_observed_runtime(
        self,
        db: Session,
        ont: object,
        summary: object,
        *,
        commit: bool = True,
    ) -> None:
        from app.services.network.ont_tr069 import OntTR069

        OntTR069._persist_observed_runtime(
            db,
            ont,
            summary,
            commit=commit,
        )
