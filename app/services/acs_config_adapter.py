"""Application-facing adapter for ACS/TR-069 ONT configuration writes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

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


class AcsConfigAdapter:
    """Keep callers from reaching into low-level ONT TR-069 action modules."""

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
            "enable_ipv6_on_wan",
        }
    )

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
        if action not in self._QUEUEABLE_ACTIONS:
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


acs_config_adapter = AcsConfigAdapter()
