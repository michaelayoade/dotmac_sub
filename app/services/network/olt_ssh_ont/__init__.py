"""OLT SSH ONT operations — modular subpackage.

This module re-exports all public functions and classes for backward compatibility.
All existing imports from `app.services.network.olt_ssh_ont` will continue to work.
"""

from app.services.network.olt_ssh_ont._common import (
    OntIphostConfig,
    OntIphostResult,
    OntStatusEntry,
    RegisteredOntEntry,
    ServicePortDiagnostics,
    _safe_profile_name,
)
from app.services.network.olt_ssh_ont.diagnostics import (
    diagnose_service_ports,
    remote_ping_ont,
)
from app.services.network.olt_ssh_ont.iphost import (
    clear_ont_ipconfig,
    configure_ont_iphost,
    configure_ont_iphost_batch,
    get_ont_iphost_config,
    parse_iphost_config_output,
)
from app.services.network.olt_ssh_ont.lifecycle import (
    _load_linked_acs_payload,
    authorize_ont,
    deauthorize_ont,
    delete_ont_registration,
    factory_reset_ont_omci,
    reboot_ont_omci,
)
from app.services.network.olt_ssh_ont.omci_config import (
    clear_ont_internet_config,
    clear_ont_wan_config,
    configure_ont_internet_config,
    configure_ont_port_native_vlan,
    configure_ont_pppoe_omci,
    configure_ont_wan_config,
)
from app.services.network.olt_ssh_ont.status import (
    find_ont_by_serial,
    get_ont_status,
    get_registered_ont_serials,
)
from app.services.network.olt_ssh_ont.tr069 import (
    bind_tr069_server_profile,
    unbind_tr069_server_profile,
)

__all__ = [
    # Dataclasses
    "OntIphostConfig",
    "OntIphostResult",
    "OntStatusEntry",
    "RegisteredOntEntry",
    "ServicePortDiagnostics",
    # Status queries
    "get_ont_status",
    "get_registered_ont_serials",
    "find_ont_by_serial",
    # IPHOST configuration
    "configure_ont_iphost",
    "configure_ont_iphost_batch",
    "get_ont_iphost_config",
    "clear_ont_ipconfig",
    "parse_iphost_config_output",
    # OMCI configuration
    "configure_ont_internet_config",
    "clear_ont_internet_config",
    "configure_ont_wan_config",
    "clear_ont_wan_config",
    "configure_ont_pppoe_omci",
    "configure_ont_port_native_vlan",
    # Lifecycle operations
    "reboot_ont_omci",
    "factory_reset_ont_omci",
    "deauthorize_ont",
    "delete_ont_registration",  # Alias for deauthorize_ont
    "authorize_ont",
    # TR-069 operations
    "bind_tr069_server_profile",
    "unbind_tr069_server_profile",
    # Diagnostics
    "diagnose_service_ports",
    "remote_ping_ont",
    # Internal helpers (re-exported for backward compatibility)
    "_safe_profile_name",
    "_load_linked_acs_payload",
]
