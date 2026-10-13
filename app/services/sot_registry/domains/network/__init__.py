"""Assemble the canonical network SOT domain from capability shards."""

from __future__ import annotations

from app.services.automation_contracts import (
    AutomationCatalogItem,
    AutomationCatalogState,
    AutomationDomainCapabilities,
)
from app.services.sot_registry.domains.network.device_operations import (
    SERVICES as DEVICE_OPERATIONS_SERVICES,
)
from app.services.sot_registry.domains.network.fiber_plant import (
    SERVICES as FIBER_PLANT_SERVICES,
)
from app.services.sot_registry.domains.network.foundation import (
    SERVICES as FOUNDATION_SERVICES,
)
from app.services.sot_registry.domains.network.infrastructure_catalogue import (
    SERVICES as INFRASTRUCTURE_CATALOGUE_SERVICES,
)
from app.services.sot_registry.domains.network.network_control import (
    SERVICES as NETWORK_CONTROL_SERVICES,
)
from app.services.sot_registry.domains.network.ont_assignments import (
    SERVICES as ONT_ASSIGNMENTS_SERVICES,
)
from app.services.sot_registry.domains.network.outage_completion_evidence import (
    SERVICES as OUTAGE_COMPLETION_EVIDENCE_SERVICES,
)
from app.services.sot_registry.domains.network.outages_and_ip import (
    SERVICES as OUTAGES_AND_IP_SERVICES,
)
from app.services.sot_registry.domains.network.subscriber_state import (
    SERVICES as SUBSCRIBER_STATE_SERVICES,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="network",
    setting_domains=(
        "network",
        "network_monitoring",
        "snmp",
        "tr069",
    ),
    services=(
        *FOUNDATION_SERVICES,
        *INFRASTRUCTURE_CATALOGUE_SERVICES,
        *FIBER_PLANT_SERVICES,
        *ONT_ASSIGNMENTS_SERVICES,
        *SUBSCRIBER_STATE_SERVICES,
        *DEVICE_OPERATIONS_SERVICES,
        *NETWORK_CONTROL_SERVICES,
        *OUTAGES_AND_IP_SERVICES,
        *OUTAGE_COMPLETION_EVIDENCE_SERVICES,
    ),
    entrypoints=(
        "app.services.topology.*",
        "app.services.infrastructure_*",
        "app.services.router_management.*",
        "app.tasks.network_*",
        "app.tasks.router_sync",
        "scripts.network.audit_fiber_topology",
        "scripts.network.review_fiber_topology_identity",
        "scripts.network.review_fiber_topology_connectivity",
        "scripts.network.review_forwarding_topology",
        "scripts.network.stage_crm_network_map",
        "scripts.network.stage_fiber_topology_kmz",
        "app.web.admin.network_*",
        "app.web.customer.connection",
        "app.api.me",
        "app.services.reseller_portal",
        "mobile",
    ),
    rule="Pollers and map collectors write observations; the fiber-topology "
    "owner validates identity and connectivity; network resolvers decide "
    "state; event services decide consequences.",
    automation=AutomationDomainCapabilities(
        catalog_items=(
            AutomationCatalogItem(
                key="provisioning.ont_commissioning",
                label="ONT commissioning",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Commissioning combines device and assignment checks owned by network services; Center steps are not registered.",
            ),
            AutomationCatalogItem(
                key="provisioning.ont_commissioning_verification",
                label="Commissioning verification",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Verification uses device readback evidence and has no approved Center trigger or action.",
            ),
            AutomationCatalogItem(
                key="provisioning.commissioned_ont_expiry_cleanup",
                label="Commissioned-ONT expiry cleanup",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Expiry cleanup is a protected network maintenance job, not an editable customer rule.",
            ),
            AutomationCatalogItem(
                key="provisioning.ont_intent_reconciliation",
                label="ONT intent reconciliation",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Reconciliation compares network intent with device state and remains in the existing network owner.",
            ),
            AutomationCatalogItem(
                key="provisioning.overdue_ont_hold_alert",
                label="Overdue ONT hold alert",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="The current alert has no registered Center trigger or notification action.",
            ),
            AutomationCatalogItem(
                key="provisioning.ont_status_signal_collection",
                label="ONT status and signal collection",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Device polling is an existing network observation process and is not a customer rule action.",
            ),
            AutomationCatalogItem(
                key="provisioning.ont_service_configuration",
                label="ONT service configuration",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Applying service settings changes a device and requires the existing assignment, authorization, and readback checks.",
            ),
            AutomationCatalogItem(
                key="provisioning.ont_firmware_upgrade",
                label="ONT firmware upgrade",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Firmware changes are privileged device operations and have no approved Center action.",
            ),
            AutomationCatalogItem(
                key="provisioning.olt_firmware_upgrade_rollback",
                label="OLT firmware upgrade or rollback",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Firmware changes are privileged device operations and remain outside customer rule actions.",
            ),
            AutomationCatalogItem(
                key="provisioning.olt_connection_retry",
                label="OLT connection retry",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Connection recovery remains in the network retry owner and is not configurable in the Center.",
            ),
            AutomationCatalogItem(
                key="provisioning.olt_mac_harvesting",
                label="OLT MAC harvesting",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="This is a network inventory observation job; Center schedule and device actions are not registered.",
            ),
            AutomationCatalogItem(
                key="provisioning.olt_profile_synchronization",
                label="OLT profile synchronization",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Profile synchronization changes device state and remains controlled by network services.",
            ),
            AutomationCatalogItem(
                key="provisioning.device_configuration_backups",
                label="Device configuration backups",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Backup scheduling and retention are protected operations, not customer-configurable rules.",
            ),
            AutomationCatalogItem(
                key="provisioning.nas_backup_retention",
                label="NAS backup retention",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Retention cleanup remains a protected maintenance job and has no Center rule contract.",
            ),
            AutomationCatalogItem(
                key="provisioning.router_configuration_readback",
                label="Router configuration read-back",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Router read-back is a protected observation process; the Center cannot yet schedule it.",
            ),
            AutomationCatalogItem(
                key="provisioning.router_source_of_truth_drift_audit",
                label="Router configuration drift audit",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Drift detection remains a read-only network audit and has no Center schedule trigger.",
            ),
            AutomationCatalogItem(
                key="provisioning.mikrotik_nas_vlan_readback",
                label="MikroTik NAS VLAN read-back",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="VLAN read-back is a network observation process and is not an editable business rule.",
            ),
            AutomationCatalogItem(
                key="provisioning.genieacs_device_synchronization",
                label="GenieACS device synchronization",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Device synchronization remains managed by the existing integration and has no Center trigger/action contract.",
            ),
            AutomationCatalogItem(
                key="provisioning.genieacs_command_reconciliation",
                label="GenieACS command reconciliation",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Command reconciliation is protected device work and is not a configurable customer rule.",
            ),
            AutomationCatalogItem(
                key="provisioning.tr069_health_runtime_refresh",
                label="TR-069 health and runtime refresh",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Health and runtime refresh remain part of the existing device integration, with no Center schedule trigger.",
            ),
            AutomationCatalogItem(
                key="provisioning.tr069_cleanup_metrics",
                label="TR-069 cleanup and metrics",
                group="Network provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Cleanup and metrics are protected maintenance work and have no Center rule contract.",
            ),
            AutomationCatalogItem(
                key="network.radius_health_monitoring",
                label="RADIUS health monitoring",
                group="Network and monitoring",
                state=AutomationCatalogState.unavailable,
                explanation="Health checks remain owned by the monitoring services and cannot yet be scheduled from Center rules.",
            ),
            AutomationCatalogItem(
                key="network.topology_status_refresh",
                label="Topology status refresh",
                group="Network and monitoring",
                state=AutomationCatalogState.unavailable,
                explanation="Topology status is refreshed by existing network services; no Center trigger or refresh action is registered.",
            ),
            AutomationCatalogItem(
                key="network.lldp_topology_polling",
                label="LLDP topology polling",
                group="Network and monitoring",
                state=AutomationCatalogState.unavailable,
                explanation="This network poll is a protected observation job, not a customer-configurable rule.",
            ),
            AutomationCatalogItem(
                key="network.uisp_topology_synchronization",
                label="UISP topology synchronization",
                group="Network and monitoring",
                state=AutomationCatalogState.unavailable,
                explanation="Synchronization remains in the UISP integration owner and has no Center schedule/action contract.",
            ),
            AutomationCatalogItem(
                key="network.ufiber_match_reporting",
                label="UFiber match reporting",
                group="Network and monitoring",
                state=AutomationCatalogState.unavailable,
                explanation="This report is produced by the existing network process; report-job support is not registered in Center.",
            ),
            AutomationCatalogItem(
                key="network.monitoring_inventory_synchronization",
                label="Monitoring inventory synchronization",
                group="Network and monitoring",
                state=AutomationCatalogState.unavailable,
                explanation="Inventory synchronization stays in the monitoring owner and cannot yet be configured as a Center rule.",
            ),
            AutomationCatalogItem(
                key="network.monitoring_coverage_refresh",
                label="Monitoring coverage refresh",
                group="Network and monitoring",
                state=AutomationCatalogState.unavailable,
                explanation="Coverage is recalculated by the monitoring owner; no safe Center trigger/action is registered.",
            ),
            AutomationCatalogItem(
                key="network.unified_device_projection_repair",
                label="Unified device projection repair",
                group="Network and monitoring",
                state=AutomationCatalogState.unavailable,
                explanation="This repair rebuilds a network projection and remains a protected maintenance job.",
            ),
            AutomationCatalogItem(
                key="network.forwarding_control_observation",
                label="Forwarding-control observations",
                group="Network and monitoring",
                state=AutomationCatalogState.unavailable,
                explanation="Forwarding observations remain in the network control owner and are not configurable rule actions.",
            ),
            AutomationCatalogItem(
                key="network.uisp_configuration_readback",
                label="UISP configuration read-back",
                group="Network and monitoring",
                state=AutomationCatalogState.unavailable,
                explanation="Read-back is an integration observation process; it has no Center trigger or schedule support.",
            ),
            AutomationCatalogItem(
                key="network.outage_detection_lifecycle",
                label="Outage detection and lifecycle",
                group="Network and monitoring",
                state=AutomationCatalogState.unavailable,
                explanation="Outage decisions affect customer impact and remain governed by the outage lifecycle owner.",
            ),
            AutomationCatalogItem(
                key="network.outage_consequences",
                label="Outage consequences",
                group="Network and monitoring",
                state=AutomationCatalogState.unavailable,
                explanation="Outage consequences use existing network and customer safeguards; configurable Center actions are not registered.",
            ),
            AutomationCatalogItem(
                key="network.automatic_outage_notification",
                label="Automatic outage notification",
                group="Network and monitoring",
                state=AutomationCatalogState.unavailable,
                explanation="Notification eligibility and affected-customer scope remain under the outage owner; no Center action is registered.",
            ),
            AutomationCatalogItem(
                key="network.infrastructure_administrator_alerts",
                label="Infrastructure administrator alerts",
                group="Network and monitoring",
                state=AutomationCatalogState.unavailable,
                explanation="Alerts use existing monitoring policy and delivery routes, not configurable Center rules yet.",
            ),
            AutomationCatalogItem(
                key="network.channel_health_observer",
                label="Channel health observer",
                group="Network and monitoring",
                state=AutomationCatalogState.unavailable,
                explanation="Channel-health observations remain in the monitoring service and have no Center schedule trigger.",
            ),
            AutomationCatalogItem(
                key="network.availability_impact_metrics",
                label="Availability and impact metrics",
                group="Network and monitoring",
                state=AutomationCatalogState.unavailable,
                explanation="These operational metrics remain generated by their existing reporting owner; Center report jobs are not available.",
            ),
            AutomationCatalogItem(
                key="network.bandwidth_processing",
                label="Bandwidth processing",
                group="Network and monitoring",
                state=AutomationCatalogState.unavailable,
                explanation="Aggregation, metrics, and retention are protected network processing jobs; no Center schedule actions are registered.",
            ),
            AutomationCatalogItem(
                key="network.ip_utilization_snapshot",
                label="IP-pool utilization snapshot",
                group="Network and monitoring",
                state=AutomationCatalogState.unavailable,
                explanation="The snapshot is an existing network reporting job; Center scheduling support is not registered.",
            ),
            AutomationCatalogItem(
                key="network.retired_zabbix_device_synchronization",
                label="Old Zabbix device synchronization",
                group="Network and monitoring",
                state=AutomationCatalogState.retired,
                explanation="This integration is retired. Developers must restore and review its owner before it can become available.",
            ),
            AutomationCatalogItem(
                key="network.retired_ont_verification_schedule",
                label="Old ONT verification schedule",
                group="Network provisioning",
                state=AutomationCatalogState.retired,
                explanation="This schedule is retired. Developers must restore and review its owner before it can become available.",
            ),
            AutomationCatalogItem(
                key="network.retired_online_silent_ont_healing",
                label="Online-silent ONT healing loop",
                group="Network provisioning",
                state=AutomationCatalogState.retired,
                explanation="The repair loop is intentionally unscheduled; recovery remains operator-controlled.",
            ),
            AutomationCatalogItem(
                key="network.retired_olt_deferred_operations",
                label="OLT deferred-operation subsystem",
                group="Network provisioning",
                state=AutomationCatalogState.retired,
                explanation="This subsystem was removed from active write paths and must be rebuilt and reviewed before use.",
            ),
            AutomationCatalogItem(
                key="network.retired_olt_hardware_discovery",
                label="OLT hardware discovery sweep",
                group="Network provisioning",
                state=AutomationCatalogState.retired,
                explanation="The old sweep is a no-op because its inventory source was retired.",
            ),
        ),
    ),
)
