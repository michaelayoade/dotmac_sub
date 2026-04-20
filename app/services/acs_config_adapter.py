"""Application-facing adapter for ACS/TR-069 ONT configuration writes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.services.adapters import adapter_registry
from app.services.network.ont_action_common import ActionResult


@dataclass(frozen=True)
class AcsConfigQueueResult:
    """Result of dispatching an ACS configuration action to the worker queue."""

    queued: bool
    action: str
    ont_id: str
    task_id: str | None = None
    task_name: str | None = None
    queue: str | None = None
    error: str | None = None

    @property
    def message(self) -> str:
        if self.queued:
            return "ACS configuration queued."
        return self.error or "ACS configuration could not be queued."


class GenieAcsConfigWriter:
    """Write ONT configuration through the current GenieACS/TR-069 backend."""

    name = "acs.config"

    _QUEUE_TASK = "app.tasks.tr069.apply_acs_config"
    _QUEUE_NAME = "acs"
    _QUEUEABLE_ACTIONS = frozenset(
        {
            "set_wifi_ssid",
            "set_wifi_password",
            "set_wifi_config",
            "toggle_lan_port",
            "set_lan_config",
            "configure_wan_config",
            "set_pppoe_credentials",
            "set_connection_request_credentials",
            "send_connection_request",
            "push_config_urgent",
            "download",
            "firmware_upgrade",
            "enable_ipv6_on_wan",
        }
    )

    @property
    def queueable_actions(self) -> frozenset[str]:
        return self._QUEUEABLE_ACTIONS

    def supports_config_action(self, action: str) -> bool:
        return action in self.queueable_actions

    def execute_config_action(
        self,
        db: Session,
        action: str,
        ont_id: str,
        *,
        args: list[object] | tuple[object, ...] | None = None,
        kwargs: dict[str, object] | None = None,
    ) -> ActionResult:
        if not self.supports_config_action(action):
            raise ValueError(f"Unsupported ACS configuration action: {action}")
        method = getattr(self, action, None)
        if method is None:
            raise ValueError(f"ACS configuration action is not implemented: {action}")
        return method(db, ont_id, *(args or ()), **dict(kwargs or {}))

    def queue_config_action(
        self,
        db: Session,
        action: str,
        ont_id: str,
        *,
        args: tuple[object, ...] | list[object] | None = None,
        kwargs: dict[str, object] | None = None,
        correlation_id: str | None = None,
        request_id: str | None = None,
        actor_id: str | None = None,
    ) -> AcsConfigQueueResult:
        """Queue an ACS configuration action for background execution."""
        _ = db
        if not self.supports_config_action(action):
            return AcsConfigQueueResult(
                queued=False,
                action=action,
                ont_id=ont_id,
                task_name=self._QUEUE_TASK,
                queue=self._QUEUE_NAME,
                error=f"Unsupported ACS configuration action: {action}",
            )

        from app.services.queue_adapter import enqueue_task

        dispatch = enqueue_task(
            self._QUEUE_TASK,
            args=(action, ont_id),
            kwargs={
                "args": list(args or ()),
                "kwargs": dict(kwargs or {}),
            },
            queue=self._QUEUE_NAME,
            correlation_id=correlation_id or f"acs_config:{ont_id}:{action}",
            source="acs_config_adapter",
            request_id=request_id,
            actor_id=actor_id,
        )
        return AcsConfigQueueResult(
            queued=dispatch.queued,
            action=action,
            ont_id=ont_id,
            task_id=dispatch.task_id,
            task_name=dispatch.task_name,
            queue=dispatch.queue,
            error=dispatch.error,
        )

    def queue_set_wifi_ssid(
        self,
        db: Session,
        ont_id: str,
        ssid: str,
        **metadata: Any,
    ) -> AcsConfigQueueResult:
        return self.queue_config_action(
            db, "set_wifi_ssid", ont_id, args=(ssid,), **metadata
        )

    def set_wifi_ssid(self, db: Session, ont_id: str, ssid: str) -> ActionResult:
        from app.services.network.ont_action_wifi import set_wifi_ssid

        return set_wifi_ssid(db, ont_id, ssid)

    def queue_set_wifi_password(
        self,
        db: Session,
        ont_id: str,
        password: str,
        **metadata: Any,
    ) -> AcsConfigQueueResult:
        return self.queue_config_action(
            db, "set_wifi_password", ont_id, args=(password,), **metadata
        )

    def set_wifi_password(
        self,
        db: Session,
        ont_id: str,
        password: str,
    ) -> ActionResult:
        from app.services.network.ont_action_wifi import set_wifi_password

        return set_wifi_password(db, ont_id, password)

    def queue_set_wifi_config(
        self,
        db: Session,
        ont_id: str,
        *,
        enabled: bool | None = None,
        ssid: str | None = None,
        password: str | None = None,
        channel: int | None = None,
        security_mode: str | None = None,
        **metadata: Any,
    ) -> AcsConfigQueueResult:
        return self.queue_config_action(
            db,
            "set_wifi_config",
            ont_id,
            kwargs={
                "enabled": enabled,
                "ssid": ssid,
                "password": password,
                "channel": channel,
                "security_mode": security_mode,
            },
            **metadata,
        )

    def set_wifi_config(
        self,
        db: Session,
        ont_id: str,
        *,
        enabled: bool | None = None,
        ssid: str | None = None,
        password: str | None = None,
        channel: int | None = None,
        security_mode: str | None = None,
    ) -> ActionResult:
        from app.services.network.ont_action_wifi import set_wifi_config

        return set_wifi_config(
            db,
            ont_id,
            enabled=enabled,
            ssid=ssid,
            password=password,
            channel=channel,
            security_mode=security_mode,
        )

    def queue_toggle_lan_port(
        self,
        db: Session,
        ont_id: str,
        port: int,
        enabled: bool,
        **metadata: Any,
    ) -> AcsConfigQueueResult:
        return self.queue_config_action(
            db, "toggle_lan_port", ont_id, args=(port, enabled), **metadata
        )

    def toggle_lan_port(
        self,
        db: Session,
        ont_id: str,
        port: int,
        enabled: bool,
    ) -> ActionResult:
        from app.services.network.ont_action_wifi import toggle_lan_port

        return toggle_lan_port(db, ont_id, port, enabled)

    def queue_set_lan_config(
        self,
        db: Session,
        ont_id: str,
        *,
        lan_ip: str | None = None,
        lan_subnet: str | None = None,
        dhcp_enabled: bool | None = None,
        dhcp_start: str | None = None,
        dhcp_end: str | None = None,
        **metadata: Any,
    ) -> AcsConfigQueueResult:
        return self.queue_config_action(
            db,
            "set_lan_config",
            ont_id,
            kwargs={
                "lan_ip": lan_ip,
                "lan_subnet": lan_subnet,
                "dhcp_enabled": dhcp_enabled,
                "dhcp_start": dhcp_start,
                "dhcp_end": dhcp_end,
            },
            **metadata,
        )

    def set_lan_config(
        self,
        db: Session,
        ont_id: str,
        *,
        lan_ip: str | None = None,
        lan_subnet: str | None = None,
        dhcp_enabled: bool | None = None,
        dhcp_start: str | None = None,
        dhcp_end: str | None = None,
    ) -> ActionResult:
        from app.services.network.ont_action_network import set_lan_config

        return set_lan_config(
            db,
            ont_id,
            lan_ip=lan_ip,
            lan_subnet=lan_subnet,
            dhcp_enabled=dhcp_enabled,
            dhcp_start=dhcp_start,
            dhcp_end=dhcp_end,
        )

    def queue_configure_wan_config(
        self,
        db: Session,
        ont_id: str,
        *,
        wan_mode: str,
        wan_vlan: int | None = None,
        ip_address: str | None = None,
        subnet_mask: str | None = None,
        gateway: str | None = None,
        dns_servers: str | None = None,
        instance_index: int = 1,
        **metadata: Any,
    ) -> AcsConfigQueueResult:
        return self.queue_config_action(
            db,
            "configure_wan_config",
            ont_id,
            kwargs={
                "wan_mode": wan_mode,
                "wan_vlan": wan_vlan,
                "ip_address": ip_address,
                "subnet_mask": subnet_mask,
                "gateway": gateway,
                "dns_servers": dns_servers,
                "instance_index": instance_index,
            },
            **metadata,
        )

    def configure_wan_config(
        self,
        db: Session,
        ont_id: str,
        *,
        wan_mode: str,
        wan_vlan: int | None = None,
        ip_address: str | None = None,
        subnet_mask: str | None = None,
        gateway: str | None = None,
        dns_servers: str | None = None,
        instance_index: int = 1,
    ) -> ActionResult:
        from app.services.network.ont_action_network import configure_wan_config

        return configure_wan_config(
            db,
            ont_id,
            wan_mode=wan_mode,
            wan_vlan=wan_vlan,
            ip_address=ip_address,
            subnet_mask=subnet_mask,
            gateway=gateway,
            dns_servers=dns_servers,
            instance_index=instance_index,
        )

    def queue_set_pppoe_credentials(
        self,
        db: Session,
        ont_id: str,
        username: str,
        password: str,
        *,
        instance_index: int = 1,
        wan_vlan: int | None = None,
        **metadata: Any,
    ) -> AcsConfigQueueResult:
        kwargs: dict[str, object] = {}
        if instance_index != 1:
            kwargs["instance_index"] = instance_index
        if wan_vlan is not None:
            kwargs["wan_vlan"] = wan_vlan
        return self.queue_config_action(
            db,
            "set_pppoe_credentials",
            ont_id,
            args=(username, password),
            kwargs=kwargs,
            **metadata,
        )

    def set_pppoe_credentials(
        self,
        db: Session,
        ont_id: str,
        username: str,
        password: str,
        *,
        instance_index: int = 1,
        wan_vlan: int | None = None,
    ) -> ActionResult:
        from app.services.network.ont_action_network import set_pppoe_credentials

        kwargs: dict[str, object] = {}
        if instance_index != 1:
            kwargs["instance_index"] = instance_index
        if wan_vlan is not None:
            kwargs["wan_vlan"] = wan_vlan
        return set_pppoe_credentials(
            db,
            ont_id,
            username,
            password,
            **kwargs,
        )

    def queue_set_connection_request_credentials(
        self,
        db: Session,
        ont_id: str,
        username: str,
        password: str,
        *,
        periodic_inform_interval: int = 3600,
        **metadata: Any,
    ) -> AcsConfigQueueResult:
        return self.queue_config_action(
            db,
            "set_connection_request_credentials",
            ont_id,
            args=(username, password),
            kwargs={"periodic_inform_interval": periodic_inform_interval},
            **metadata,
        )

    def set_connection_request_credentials(
        self,
        db: Session,
        ont_id: str,
        username: str,
        password: str,
        *,
        periodic_inform_interval: int = 3600,
    ) -> ActionResult:
        from app.services.network.ont_action_network import (
            set_connection_request_credentials,
        )

        return set_connection_request_credentials(
            db,
            ont_id,
            username,
            password,
            periodic_inform_interval=periodic_inform_interval,
        )

    def queue_send_connection_request(
        self,
        db: Session,
        ont_id: str,
        **metadata: Any,
    ) -> AcsConfigQueueResult:
        return self.queue_config_action(
            db, "send_connection_request", ont_id, **metadata
        )

    def send_connection_request(self, db: Session, ont_id: str) -> ActionResult:
        from app.services.network.ont_action_network import send_connection_request

        return send_connection_request(db, ont_id)

    def queue_push_config_urgent(
        self,
        db: Session,
        ont_id: str,
        parameters: dict[str, Any],
        *,
        expected: dict[str, Any] | None = None,
        **metadata: Any,
    ) -> AcsConfigQueueResult:
        kwargs: dict[str, object] = {"parameters": parameters}
        if expected is not None:
            kwargs["expected"] = expected
        return self.queue_config_action(
            db,
            "push_config_urgent",
            ont_id,
            kwargs=kwargs,
            **metadata,
        )

    def push_config_urgent(
        self,
        db: Session,
        ont_id: str,
        parameters: dict[str, Any],
        *,
        expected: dict[str, Any] | None = None,
        connection_request_attempts: int = 3,
        connection_request_backoff_sec: float = 1.0,
    ) -> ActionResult:
        """Push raw ACS parameters and force immediate device processing."""
        from app.services.genieacs import GenieACSError
        from app.services.network.ont_action_common import (
            get_ont_client_or_error,
            set_and_verify,
        )

        if not parameters:
            return ActionResult(success=False, message="No parameters supplied.")

        resolved, error = get_ont_client_or_error(db, ont_id)
        if error:
            return error
        if resolved is None:
            return ActionResult(
                success=False,
                message="No ACS device resolved for this ONT.",
            )
        _ont, client, device_id = resolved

        normalized_parameters = {
            str(path): value for path, value in parameters.items()
        }
        normalized_expected = (
            {str(path): value for path, value in expected.items()}
            if expected is not None
            else None
        )
        try:
            task = set_and_verify(
                client,
                device_id,
                normalized_parameters,
                expected=normalized_expected,
                connection_request_attempts=connection_request_attempts,
                connection_request_backoff_sec=connection_request_backoff_sec,
            )
        except GenieACSError as exc:
            return ActionResult(
                success=False,
                message=f"Urgent ACS config push failed: {exc}",
            )
        return ActionResult(
            success=True,
            message="Urgent ACS config pushed and verified.",
            data={
                "device_id": device_id,
                "parameters": list(normalized_parameters),
                "task": task,
                "connection_request": True,
                "connection_request_attempts": max(1, int(connection_request_attempts)),
            },
        )

    def queue_download(
        self,
        db: Session,
        ont_id: str,
        *,
        file_type: str,
        file_url: str,
        filename: str | None = None,
        **metadata: Any,
    ) -> AcsConfigQueueResult:
        return self.queue_config_action(
            db,
            "download",
            ont_id,
            kwargs={
                "file_type": file_type,
                "file_url": file_url,
                "filename": filename,
            },
            **metadata,
        )

    def download(
        self,
        db: Session,
        ont_id: str,
        *,
        file_type: str,
        file_url: str,
        filename: str | None = None,
    ) -> ActionResult:
        """Trigger an ACS Download RPC for an ONT."""
        from app.services.genieacs import GenieACSError
        from app.services.network.ont_action_common import get_ont_client_or_error

        if not file_type.strip():
            return ActionResult(success=False, message="Download file type is required.")
        if not file_url.strip():
            return ActionResult(success=False, message="Download file URL is required.")

        resolved, error = get_ont_client_or_error(db, ont_id)
        if error:
            return error
        if resolved is None:
            return ActionResult(success=False, message="No ACS device resolved.")
        ont, client, device_id = resolved

        try:
            task = client.download(
                device_id,
                file_type=file_type.strip(),
                file_url=file_url.strip(),
                filename=filename.strip() if filename else None,
                connection_request=True,
            )
        except GenieACSError as exc:
            return ActionResult(success=False, message=f"ACS download failed: {exc}")

        return ActionResult(
            success=True,
            message=f"ACS download queued for {ont.serial_number}.",
            data={
                "device_id": device_id,
                "file_type": file_type.strip(),
                "file_url": file_url.strip(),
                "filename": filename.strip() if filename else None,
                "task": task,
                "connection_request": True,
            },
        )

    def queue_firmware_upgrade(
        self,
        db: Session,
        ont_id: str,
        firmware_image_id: str,
        **metadata: Any,
    ) -> AcsConfigQueueResult:
        return self.queue_config_action(
            db,
            "firmware_upgrade",
            ont_id,
            args=(firmware_image_id,),
            **metadata,
        )

    def firmware_upgrade(
        self, db: Session, ont_id: str, firmware_image_id: str
    ) -> ActionResult:
        """Trigger ONT firmware download through the configured ACS backend."""
        from app.models.network import OntFirmwareImage

        firmware = db.get(OntFirmwareImage, firmware_image_id)
        if firmware is None:
            return ActionResult(success=False, message="Firmware image not found.")
        if not firmware.is_active:
            return ActionResult(success=False, message="Firmware image is not active.")

        result = self.download(
            db,
            ont_id,
            file_type="1 Firmware Upgrade Image",
            file_url=firmware.file_url,
            filename=firmware.filename,
        )
        if not result.success:
            return result

        data = dict(result.data or {})
        data.update(
            {
                "firmware_image_id": str(firmware.id),
                "firmware_vendor": firmware.vendor,
                "firmware_model": firmware.model,
                "firmware_version": firmware.version,
                "checksum": firmware.checksum,
                "file_size_bytes": firmware.file_size_bytes,
            }
        )
        return ActionResult(
            success=True,
            message=(
                f"Firmware upgrade to v{firmware.version} initiated. "
                "The ONT will download the image and reboot if the device accepts it."
            ),
            data=data,
        )

    def queue_enable_ipv6_on_wan(
        self,
        db: Session,
        ont_id: str,
        *,
        wan_instance: int = 1,
        **metadata: Any,
    ) -> AcsConfigQueueResult:
        return self.queue_config_action(
            db,
            "enable_ipv6_on_wan",
            ont_id,
            kwargs={"wan_instance": wan_instance},
            **metadata,
        )

    def enable_ipv6_on_wan(
        self,
        db: Session,
        ont_id: str,
        *,
        wan_instance: int = 1,
    ) -> ActionResult:
        from app.services.network.ont_action_network import enable_ipv6_on_wan

        return enable_ipv6_on_wan(db, ont_id, wan_instance=wan_instance)


AcsConfigAdapter = GenieAcsConfigWriter
acs_config_adapter = GenieAcsConfigWriter()
adapter_registry.register(acs_config_adapter)
