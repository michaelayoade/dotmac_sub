"""Web service helpers for remote ONT action routes.

This package provides services for ONT configuration, diagnostics,
and operational actions exposed through the admin web UI.

All existing import patterns are preserved for backward compatibility:
    from app.services import web_network_ont_actions as web_network_ont_actions_service
    from app.services.web_network_ont_actions import return_to_inventory
    from app.services.web_network_ont_actions import _cleanup_olt_state_for_return
"""

# Re-export private helpers that are imported externally
from app.services.web_network_ont_actions._common import (
    _actor_id_from_request,
    _config_snapshot_service,
    _current_user,
    _display_olt_value,
    _intent_saved_result,
    _is_input_error,
    _log_action_audit,
    _normalize_fsp,
    _parse_ont_id_on_olt,
    _persist_ont_plan_step,
    _persist_wan_intent,
    _resolve_return_olt_context,
    actor_name_from_request,
)

# Configuration setters
from app.services.web_network_ont_actions.config_setters import (
    bind_tr069_profile,
    configure_management_ip,
    configure_wan_config,
    configure_wan_with_pppoe,
    set_lan_config,
    set_pppoe_credentials,
    set_wifi_config,
    set_wifi_password,
    set_wifi_ssid,
    toggle_lan_port,
)

# Context builders
from app.services.web_network_ont_actions.context_builders import (
    configure_form_context,
    iphost_config_context,
    lan_config_context,
    olt_side_config_context,
    olt_status_context,
    tr069_profile_config_context,
    unified_config_context,
    wan_config_context,
    wifi_config_context,
)

# Credentials
from app.services.web_network_ont_actions.credentials import (
    resolve_stored_pppoe_password,
    reveal_stored_pppoe_password,
)

# Database configuration
from app.services.web_network_ont_actions.db_config import (
    update_ont_config,
)

# Device actions
from app.services.web_network_ont_actions.device_actions import (
    execute_config_snapshot_refresh,
    execute_connection_request,
    execute_enable_ipv6,
    execute_factory_reset,
    execute_omci_reboot,
    execute_reboot,
    execute_refresh,
)

# Diagnostics
from app.services.web_network_ont_actions.diagnostics import (
    fetch_iphost_config,
    fetch_running_config,
    run_ping_diagnostic,
    run_traceroute_diagnostic,
    running_config_context,
)

# Inventory management
from app.services.web_network_ont_actions.inventory import (
    _cleanup_olt_state_for_return,
    apply_profile,
    firmware_upgrade,
    return_to_inventory,
    return_to_inventory_for_web,
)

# Operational health and runbook
from app.services.web_network_ont_actions.operational import (
    fetch_olt_side_config,
    fetch_olt_status,
    operational_health_context,
    reconcile_operational_state,
)

# Config snapshots
from app.services.web_network_ont_actions.snapshots import (
    capture_config_snapshot_list_context,
    config_snapshot_detail_context,
    delete_config_snapshot_list_context,
)

__all__ = [
    # Private helpers (needed by external modules)
    "_actor_id_from_request",
    "_cleanup_olt_state_for_return",
    "_config_snapshot_service",
    "_current_user",
    "_display_olt_value",
    "_intent_saved_result",
    "_is_input_error",
    "_log_action_audit",
    "_normalize_fsp",
    "_parse_ont_id_on_olt",
    "_persist_ont_plan_step",
    "_persist_wan_intent",
    "_resolve_return_olt_context",
    # Public helpers
    "actor_name_from_request",
    # Credentials
    "resolve_stored_pppoe_password",
    "reveal_stored_pppoe_password",
    # Config snapshots
    "capture_config_snapshot_list_context",
    "config_snapshot_detail_context",
    "delete_config_snapshot_list_context",
    # Diagnostics
    "fetch_iphost_config",
    "fetch_running_config",
    "run_ping_diagnostic",
    "run_traceroute_diagnostic",
    "running_config_context",
    # Device actions
    "execute_config_snapshot_refresh",
    "execute_connection_request",
    "execute_enable_ipv6",
    "execute_factory_reset",
    "execute_omci_reboot",
    "execute_reboot",
    "execute_refresh",
    # Configuration setters
    "bind_tr069_profile",
    "configure_management_ip",
    "configure_wan_config",
    "configure_wan_with_pppoe",
    "set_lan_config",
    "set_pppoe_credentials",
    "set_wifi_config",
    "set_wifi_password",
    "set_wifi_ssid",
    "toggle_lan_port",
    # Context builders
    "configure_form_context",
    "iphost_config_context",
    "lan_config_context",
    "olt_side_config_context",
    "olt_status_context",
    "tr069_profile_config_context",
    "unified_config_context",
    "wan_config_context",
    "wifi_config_context",
    # Inventory
    "apply_profile",
    "firmware_upgrade",
    "return_to_inventory",
    "return_to_inventory_for_web",
    # Operational
    "fetch_olt_side_config",
    "fetch_olt_status",
    "operational_health_context",
    "reconcile_operational_state",
    # Database config
    "update_ont_config",
]
