"""Service helpers for remote ONT action web routes."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.network.ont_actions import ActionResult, OntActions

logger = logging.getLogger(__name__)


def execute_reboot(db: Session, ont_id: str) -> ActionResult:
    """Execute reboot action and return result."""
    return OntActions.reboot(db, ont_id)


def execute_refresh(db: Session, ont_id: str) -> ActionResult:
    """Execute status refresh and return result."""
    return OntActions.refresh_status(db, ont_id)


def fetch_running_config(db: Session, ont_id: str) -> ActionResult:
    """Fetch running config and return structured result."""
    return OntActions.get_running_config(db, ont_id)


def execute_factory_reset(db: Session, ont_id: str) -> ActionResult:
    """Execute factory reset and return result."""
    return OntActions.factory_reset(db, ont_id)


def set_wifi_ssid(db: Session, ont_id: str, ssid: str) -> ActionResult:
    """Set WiFi SSID and return result."""
    return OntActions.set_wifi_ssid(db, ont_id, ssid)


def set_wifi_password(db: Session, ont_id: str, password: str) -> ActionResult:
    """Set WiFi password and return result."""
    return OntActions.set_wifi_password(db, ont_id, password)


def toggle_lan_port(
    db: Session, ont_id: str, port: int, enabled: bool
) -> ActionResult:
    """Toggle a LAN port and return result."""
    return OntActions.toggle_lan_port(db, ont_id, port, enabled)


def set_pppoe_credentials(
    db: Session, ont_id: str, username: str, password: str
) -> ActionResult:
    """Push PPPoE credentials to ONT via TR-069."""
    return OntActions.set_pppoe_credentials(db, ont_id, username, password)


def run_ping_diagnostic(
    db: Session, ont_id: str, host: str, count: int = 4
) -> ActionResult:
    """Run ping diagnostic from ONT via TR-069."""
    return OntActions.run_ping_diagnostic(db, ont_id, host, count)


def run_traceroute_diagnostic(
    db: Session, ont_id: str, host: str
) -> ActionResult:
    """Run traceroute diagnostic from ONT via TR-069."""
    return OntActions.run_traceroute_diagnostic(db, ont_id, host)


def execute_omci_reboot(db: Session, ont_id: str) -> tuple[bool, str]:
    """Reboot ONT via OMCI through the OLT."""
    from app.services.network.olt_ssh_ont import reboot_ont_omci
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT"
    return reboot_ont_omci(olt, fsp, olt_ont_id)


def configure_management_ip(
    db: Session,
    ont_id: str,
    vlan_id: int,
    ip_mode: str = "dhcp",
    ip_address: str | None = None,
    subnet: str | None = None,
    gateway: str | None = None,
) -> tuple[bool, str]:
    """Configure ONT management IP via OLT IPHOST command."""
    from app.services.network.olt_ssh_ont import configure_ont_iphost
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT"
    return configure_ont_iphost(
        olt, fsp, olt_ont_id,
        vlan_id=vlan_id, ip_mode=ip_mode,
        ip_address=ip_address, subnet=subnet, gateway=gateway,
    )


def fetch_iphost_config(db: Session, ont_id: str) -> tuple[bool, str, dict[str, str]]:
    """Fetch ONT IPHOST config from OLT."""
    from app.services.network.olt_ssh_ont import get_ont_iphost_config
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT", {}
    return get_ont_iphost_config(olt, fsp, olt_ont_id)


def bind_tr069_profile(
    db: Session, ont_id: str, profile_id: int
) -> tuple[bool, str]:
    """Bind TR-069 server profile to ONT via OLT."""
    from app.services.network.olt_ssh_ont import bind_tr069_server_profile
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT"
    return bind_tr069_server_profile(olt, fsp, olt_ont_id, profile_id)
