"""Application-facing adapter for OLT operational actions."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services.adapters import adapter_registry


class OltActionAdapter:
    """Keep OLT UI flows behind the operational OLT boundary."""

    name = "olt_action"
    depends_on: tuple[str, ...] = ("db.session.sqlalchemy",)

    def fetch_running_config(self, olt: object, db: Session | None = None) -> str | None:
        from app.services.network import olt_operations as olt_operations_service

        return olt_operations_service.fetch_running_config(olt, db=db)

    def get_olt_firmware_images(self, db: Session, olt_id: str) -> list:
        from app.services.network import olt_operations as olt_operations_service

        return olt_operations_service.get_olt_firmware_images(db, olt_id)

    def execute_cli_command(
        self, db: Session, olt_id: str, command: str, **kwargs: Any
    ) -> tuple[bool, str, str]:
        from app.services.network import olt_operations as olt_operations_service

        return olt_operations_service.execute_cli_command(db, olt_id, command, **kwargs)

    def get_ont_status_by_serial(
        self, db: Session, olt_id: str, serial: str, **kwargs: Any
    ) -> tuple[bool, str, dict[str, object]]:
        from app.services.network import olt_operations as olt_operations_service

        return olt_operations_service.get_ont_status_by_serial(
            db, olt_id, serial, **kwargs
        )

    def test_olt_ssh_connection(
        self, db: Session, olt_id: str, **kwargs: Any
    ) -> tuple[bool, str, str | None]:
        from app.services.network import olt_operations as olt_operations_service

        return olt_operations_service.test_olt_ssh_connection(db, olt_id, **kwargs)

    def test_olt_snmp_connection(
        self, db: Session, olt_id: str, **kwargs: Any
    ) -> tuple[bool, str]:
        from app.services.network import olt_operations as olt_operations_service

        return olt_operations_service.test_olt_snmp_connection(db, olt_id, **kwargs)

    def test_olt_netconf_connection(
        self, db: Session, olt_id: str, **kwargs: Any
    ) -> tuple[bool, str, list[str]]:
        from app.services.network import olt_operations as olt_operations_service

        return olt_operations_service.test_olt_netconf_connection(db, olt_id, **kwargs)

    def get_olt_netconf_config(
        self, db: Session, olt_id: str, **kwargs: Any
    ) -> tuple[bool, str, str]:
        from app.services.network import olt_operations as olt_operations_service

        return olt_operations_service.get_olt_netconf_config(db, olt_id, **kwargs)

    def fetch_running_config_ssh_preview(
        self, db: Session, olt_id: str, **kwargs: Any
    ) -> tuple[bool, str, str]:
        from app.services.network import olt_operations as olt_operations_service

        return olt_operations_service.fetch_running_config_ssh_preview(
            db, olt_id, **kwargs
        )

    def backup_running_config_ssh(
        self, db: Session, olt_id: str, **kwargs: Any
    ) -> tuple[object, str]:
        from app.services.network import olt_operations as olt_operations_service

        return olt_operations_service.backup_running_config_ssh(db, olt_id, **kwargs)

    def execute_authorization(
        self,
        db: Session,
        olt_id: str,
        fsp: str,
        serial_number: str,
        *,
        force_reauthorize: bool = False,
        **kwargs: Any,
    ) -> object:
        from app.services.network.authorization_executor import execute_authorization

        return execute_authorization(
            db,
            olt_id,
            fsp,
            serial_number,
            force_reauthorize=force_reauthorize,
            **kwargs,
        )

    def authorize_ont(
        self,
        db: Session,
        *,
        olt_id: str,
        fsp: str,
        serial_number: str,
        force_reauthorize: bool = False,
        preset_id: str | None = None,
        request: object | None = None,
        **kwargs: Any,
    ) -> tuple[bool, str, str | None]:
        """Authorize an ONT synchronously.

        Returns:
            Tuple of (success, message, ont_unit_id).
        """
        from app.services.network.ont_authorization import (
            authorize_autofind_ont_and_provision_network_audited,
        )

        result = authorize_autofind_ont_and_provision_network_audited(
            db,
            olt_id,
            fsp,
            serial_number,
            force_reauthorize=force_reauthorize,
            preset_id=preset_id,
            request=request,
        )
        return (result.success, result.message, result.ont_unit_id)


olt_action_adapter = OltActionAdapter()
adapter_registry.register(olt_action_adapter)
