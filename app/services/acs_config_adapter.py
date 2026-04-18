"""Application-facing adapter for ACS/TR-069 ONT configuration writes."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.network.ont_action_common import ActionResult


class AcsConfigAdapter:
    """Keep callers from reaching into low-level ONT TR-069 action modules."""

    def set_wifi_ssid(self, db: Session, ont_id: str, ssid: str) -> ActionResult:
        from app.services.network.ont_action_wifi import set_wifi_ssid

        return set_wifi_ssid(db, ont_id, ssid)

    def set_wifi_password(
        self,
        db: Session,
        ont_id: str,
        password: str,
    ) -> ActionResult:
        from app.services.network.ont_action_wifi import set_wifi_password

        return set_wifi_password(db, ont_id, password)

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

    def toggle_lan_port(
        self,
        db: Session,
        ont_id: str,
        port: int,
        enabled: bool,
    ) -> ActionResult:
        from app.services.network.ont_action_wifi import toggle_lan_port

        return toggle_lan_port(db, ont_id, port, enabled)

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

    def send_connection_request(self, db: Session, ont_id: str) -> ActionResult:
        from app.services.network.ont_action_network import send_connection_request

        return send_connection_request(db, ont_id)

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
