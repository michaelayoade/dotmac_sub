"""Service for managing GenieACS presets (declarative auto-config templates)."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.tr069 import Tr069AcsServer
from app.services.acs_client import AcsClient, create_acs_client
from app.services.genieacs import GenieACSError
from app.services.tr069_web_audit import log_tr069_audit_event

logger = logging.getLogger(__name__)


class Tr069PresetManager:
    """Manager for GenieACS preset operations."""

    def _get_client(self, db: Session, acs_server_id: str) -> AcsClient:
        """Get a GenieACS client for the specified ACS server."""
        server = db.get(Tr069AcsServer, acs_server_id)
        if not server:
            raise ValueError(f"ACS server not found: {acs_server_id}")
        return create_acs_client(server.base_url)

    def list(self, db: Session, acs_server_id: str) -> list[dict[str, Any]]:
        """List all presets from GenieACS.

        Args:
            db: Database session
            acs_server_id: ACS server UUID

        Returns:
            List of preset documents
        """
        client = self._get_client(db, acs_server_id)
        try:
            return client.list_presets()
        except GenieACSError as e:
            logger.error("Failed to list presets from GenieACS: %s", e)
            raise

    def get(self, db: Session, acs_server_id: str, preset_id: str) -> dict[str, Any]:
        """Get a single preset by ID.

        Args:
            db: Database session
            acs_server_id: ACS server UUID
            preset_id: Preset ID (name)

        Returns:
            Preset document
        """
        client = self._get_client(db, acs_server_id)
        try:
            return client.get_preset(preset_id)
        except GenieACSError as e:
            logger.error("Failed to get preset %s from GenieACS: %s", preset_id, e)
            raise

    def create(
        self,
        db: Session,
        acs_server_id: str,
        preset_data: dict[str, Any],
        *,
        request: Request | None = None,
    ) -> dict[str, Any]:
        """Create a new preset.

        Args:
            db: Database session
            acs_server_id: ACS server UUID
            preset_data: Preset definition including _id

        Returns:
            Created preset document
        """
        if not preset_data.get("_id"):
            raise ValueError("Preset _id is required")

        client = self._get_client(db, acs_server_id)
        try:
            result = client.create_preset(preset_data)
            log_tr069_audit_event(
                db,
                request=request,
                action="create",
                entity_type="tr069_preset",
                entity_id=preset_data["_id"],
                metadata={"acs_server_id": acs_server_id},
            )
            return result
        except GenieACSError as e:
            logger.error("Failed to create preset in GenieACS: %s", e)
            raise

    def update(
        self,
        db: Session,
        acs_server_id: str,
        preset_id: str,
        preset_data: dict[str, Any],
        *,
        request: Request | None = None,
    ) -> dict[str, Any]:
        """Update an existing preset.

        Args:
            db: Database session
            acs_server_id: ACS server UUID
            preset_id: Preset ID (name)
            preset_data: Updated preset definition

        Returns:
            Updated preset document
        """
        # Ensure _id matches the preset_id
        preset_data["_id"] = preset_id

        client = self._get_client(db, acs_server_id)
        try:
            result = client.create_preset(preset_data)  # PUT is idempotent in GenieACS
            log_tr069_audit_event(
                db,
                request=request,
                action="update",
                entity_type="tr069_preset",
                entity_id=preset_id,
                metadata={"acs_server_id": acs_server_id},
            )
            return result
        except GenieACSError as e:
            logger.error("Failed to update preset %s in GenieACS: %s", preset_id, e)
            raise

    def delete(
        self,
        db: Session,
        acs_server_id: str,
        preset_id: str,
        *,
        request: Request | None = None,
    ) -> bool:
        """Delete a preset.

        Args:
            db: Database session
            acs_server_id: ACS server UUID
            preset_id: Preset ID (name)

        Returns:
            True if deleted successfully
        """
        client = self._get_client(db, acs_server_id)
        try:
            client.delete_preset(preset_id)
            log_tr069_audit_event(
                db,
                request=request,
                action="delete",
                entity_type="tr069_preset",
                entity_id=preset_id,
                metadata={"acs_server_id": acs_server_id},
            )
            return True
        except GenieACSError as e:
            logger.error("Failed to delete preset %s from GenieACS: %s", preset_id, e)
            raise


# Singleton instance
presets = Tr069PresetManager()
