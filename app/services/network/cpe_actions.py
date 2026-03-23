"""Compatibility facade for CPE device actions.

Focused implementations live in sibling modules grouped by responsibility.
"""

from __future__ import annotations

from app.services.network.cpe_action_device import (
    factory_reset,
    get_running_config,
    reboot,
    refresh_status,
)
from app.services.network.cpe_action_diagnostics import (
    run_ping_diagnostic,
    run_traceroute_diagnostic,
)
from app.services.network.cpe_action_network import (
    send_connection_request,
    set_connection_request_credentials,
)
from app.services.network.cpe_action_wifi import (
    set_wifi_password,
    set_wifi_ssid,
    toggle_lan_port,
)
from app.services.network.ont_action_common import ActionResult, DeviceConfig


class CpeActions:
    """Remote CPE device action dispatcher using focused action modules."""

    reboot = staticmethod(reboot)
    refresh_status = staticmethod(refresh_status)
    get_running_config = staticmethod(get_running_config)
    factory_reset = staticmethod(factory_reset)
    set_wifi_ssid = staticmethod(set_wifi_ssid)
    set_wifi_password = staticmethod(set_wifi_password)
    toggle_lan_port = staticmethod(toggle_lan_port)
    set_connection_request_credentials = staticmethod(
        set_connection_request_credentials
    )
    send_connection_request = staticmethod(send_connection_request)
    run_ping_diagnostic = staticmethod(run_ping_diagnostic)
    run_traceroute_diagnostic = staticmethod(run_traceroute_diagnostic)


cpe_actions = CpeActions()

__all__ = [
    "ActionResult",
    "CpeActions",
    "DeviceConfig",
    "cpe_actions",
]
