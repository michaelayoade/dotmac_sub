"""Service for managing GenieACS provisions (JavaScript config scripts)."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.tr069 import Tr069AcsServer
from app.services.genieacs import GenieACSClient, GenieACSError
from app.services.tr069_web_audit import log_tr069_audit_event

logger = logging.getLogger(__name__)


class Tr069ProvisionManager:
    """Manager for GenieACS provision operations."""

    def _get_client(self, db: Session, acs_server_id: str) -> GenieACSClient:
        """Get a GenieACS client for the specified ACS server."""
        server = db.get(Tr069AcsServer, acs_server_id)
        if not server:
            raise ValueError(f"ACS server not found: {acs_server_id}")
        return GenieACSClient(server.base_url)

    def list(self, db: Session, acs_server_id: str) -> list[dict[str, Any]]:
        """List all provisions from GenieACS.

        Args:
            db: Database session
            acs_server_id: ACS server UUID

        Returns:
            List of provision documents
        """
        client = self._get_client(db, acs_server_id)
        try:
            return client.list_provisions()
        except GenieACSError as e:
            logger.error("Failed to list provisions from GenieACS: %s", e)
            raise

    def get(self, db: Session, acs_server_id: str, provision_id: str) -> dict[str, Any]:
        """Get a single provision by ID.

        Args:
            db: Database session
            acs_server_id: ACS server UUID
            provision_id: Provision ID (name)

        Returns:
            Provision document
        """
        client = self._get_client(db, acs_server_id)
        try:
            return client.get_provision(provision_id)
        except GenieACSError as e:
            logger.error(
                "Failed to get provision %s from GenieACS: %s", provision_id, e
            )
            raise

    def create(
        self,
        db: Session,
        acs_server_id: str,
        provision_id: str,
        script: str,
        *,
        request: Request | None = None,
    ) -> None:
        """Create a new provision.

        Args:
            db: Database session
            acs_server_id: ACS server UUID
            provision_id: Provision ID (name)
            script: JavaScript provision script
        """
        if not provision_id or not provision_id.strip():
            raise ValueError("Provision ID is required")
        if not script or not script.strip():
            raise ValueError("Provision script is required")

        client = self._get_client(db, acs_server_id)
        try:
            client.create_provision(provision_id.strip(), script)
            log_tr069_audit_event(
                db,
                request=request,
                action="create",
                entity_type="tr069_provision",
                entity_id=provision_id,
                metadata={"acs_server_id": acs_server_id},
            )
        except GenieACSError as e:
            logger.error("Failed to create provision in GenieACS: %s", e)
            raise

    def update(
        self,
        db: Session,
        acs_server_id: str,
        provision_id: str,
        script: str,
        *,
        request: Request | None = None,
    ) -> None:
        """Update an existing provision.

        Args:
            db: Database session
            acs_server_id: ACS server UUID
            provision_id: Provision ID (name)
            script: JavaScript provision script
        """
        if not script or not script.strip():
            raise ValueError("Provision script is required")

        client = self._get_client(db, acs_server_id)
        try:
            client.create_provision(provision_id, script)  # PUT is idempotent
            log_tr069_audit_event(
                db,
                request=request,
                action="update",
                entity_type="tr069_provision",
                entity_id=provision_id,
                metadata={"acs_server_id": acs_server_id},
            )
        except GenieACSError as e:
            logger.error(
                "Failed to update provision %s in GenieACS: %s", provision_id, e
            )
            raise

    def delete(
        self,
        db: Session,
        acs_server_id: str,
        provision_id: str,
        *,
        request: Request | None = None,
    ) -> bool:
        """Delete a provision.

        Args:
            db: Database session
            acs_server_id: ACS server UUID
            provision_id: Provision ID (name)

        Returns:
            True if deleted successfully
        """
        client = self._get_client(db, acs_server_id)
        try:
            client.delete_provision(provision_id)
            log_tr069_audit_event(
                db,
                request=request,
                action="delete",
                entity_type="tr069_provision",
                entity_id=provision_id,
                metadata={"acs_server_id": acs_server_id},
            )
            return True
        except GenieACSError as e:
            logger.error(
                "Failed to delete provision %s from GenieACS: %s", provision_id, e
            )
            raise


# Singleton instance
provisions = Tr069ProvisionManager()
