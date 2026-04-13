"""Compatibility facade for ONT actions.

Focused implementations live in sibling modules grouped by responsibility.
"""

from __future__ import annotations

from app.services.network.ont_action_common import ActionResult, DeviceConfig
from app.services.network.ont_action_device import (
    factory_reset,
    firmware_upgrade,
    get_running_config,
    reboot,
    refresh_status,
)
from app.services.network.ont_action_diagnostics import (
    run_ping_diagnostic,
    run_traceroute_diagnostic,
)
from app.services.network.ont_action_network import (
    configure_wan_config,
    enable_ipv6_on_wan,
    send_connection_request,
    set_connection_request_credentials,
    set_lan_config,
    set_pppoe_credentials,
)
from app.services.network.ont_action_wifi import (
    set_wifi_config,
    set_wifi_password,
    set_wifi_ssid,
    toggle_lan_port,
)


class OntActions:
    """Remote ONT action dispatcher using focused action modules."""

    reboot = staticmethod(reboot)
    refresh_status = staticmethod(refresh_status)
    get_running_config = staticmethod(get_running_config)
    factory_reset = staticmethod(factory_reset)
    firmware_upgrade = staticmethod(firmware_upgrade)
    set_wifi_ssid = staticmethod(set_wifi_ssid)
    set_wifi_password = staticmethod(set_wifi_password)
    set_wifi_config = staticmethod(set_wifi_config)
    toggle_lan_port = staticmethod(toggle_lan_port)
    set_pppoe_credentials = staticmethod(set_pppoe_credentials)
    set_connection_request_credentials = staticmethod(
        set_connection_request_credentials
    )
    send_connection_request = staticmethod(send_connection_request)
    run_ping_diagnostic = staticmethod(run_ping_diagnostic)
    run_traceroute_diagnostic = staticmethod(run_traceroute_diagnostic)
    enable_ipv6_on_wan = staticmethod(enable_ipv6_on_wan)
    set_lan_config = staticmethod(set_lan_config)
    configure_wan_config = staticmethod(configure_wan_config)


ont_actions = OntActions()

__all__ = [
    "ActionResult",
    "DeviceConfig",
    "OntActions",
    "ont_actions",
]
