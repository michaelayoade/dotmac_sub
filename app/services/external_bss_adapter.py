"""External OSS/BSS boundary for internal services."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.external import ExternalEntityType
from app.models.splynx_mapping import SplynxEntityType
from app.schemas.external import ExternalReferenceSync


@dataclass(frozen=True)
class ExternalBssReference:
    connector_config_id: UUID
    entity_type: ExternalEntityType
    entity_id: UUID
    external_id: str
    external_url: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    is_active: bool | None = True


class ExternalBssAdapter:
    """Adapter for external OSS/BSS references and legacy Splynx IDs."""

    def build_reference_payload(
        self,
        reference: ExternalBssReference,
    ) -> ExternalReferenceSync:
        return ExternalReferenceSync(
            connector_config_id=reference.connector_config_id,
            entity_type=reference.entity_type,
            entity_id=reference.entity_id,
            external_id=reference.external_id,
            external_url=reference.external_url,
            metadata_=dict(reference.metadata or {}),
            is_active=reference.is_active,
        )

    def sync_reference(self, db: Session, reference: ExternalBssReference):
        from app.services.external import sync_reference

        return sync_reference(db, self.build_reference_payload(reference))

    def register_splynx_mapping(
        self,
        db: Session,
        *,
        entity_type: SplynxEntityType,
        splynx_id: int,
        dotmac_id: str | UUID,
        metadata: dict[str, object] | None = None,
    ):
        from app.services.splynx_mapping import splynx_mapping

        return splynx_mapping.register_or_update(
            db,
            entity_type,
            splynx_id,
            dotmac_id,
            metadata=dict(metadata or {}),
        )

    def lookup_splynx_id(
        self,
        db: Session,
        *,
        entity_type: SplynxEntityType,
        dotmac_id: str | UUID,
    ) -> int | None:
        from app.services.splynx_mapping import splynx_mapping

        return splynx_mapping.lookup_by_dotmac(db, entity_type, dotmac_id)


external_bss_adapter = ExternalBssAdapter()

