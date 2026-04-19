"""Pre-built saga workflows for ONT provisioning.

This module provides ready-to-use saga definitions for common
provisioning scenarios, wrapping existing step functions with
proper compensation actions.

Available Sagas:
- INTERNET_PROVISIONING_SAGA: Full internet service provisioning
- WIFI_SETUP_SAGA: WiFi-only configuration
- FULL_PROVISIONING_SAGA: Complete provisioning with all steps
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.services.network.ont_provisioning.result import StepResult
from app.services.network.ont_provisioning.saga.types import (
    SagaContext,
    SagaDefinition,
    SagaStep,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step Action Wrappers
# ---------------------------------------------------------------------------
# These wrap existing step functions to match the SagaStep action signature.
# Each function takes SagaContext and returns StepResult.


def _create_service_ports(ctx: SagaContext) -> StepResult:
    """Create service ports for internet VLAN.

    Uses step_data keys:
    - internet_vlan_id: VLAN for internet service (required)
    - gem_index: GEM port index (default: 1)
    - user_vlan: User-side VLAN (optional)
    - tag_transform: Tag transform mode (default: "translate")
    """
    from app.services.network import ont_provision_steps

    vlan_id = ctx.step_data.get("internet_vlan_id")
    if vlan_id is None:
        return StepResult(
            step_name="create_service_ports",
            success=False,
            message="internet_vlan_id not provided in step_data",
            critical=True,
        )

    result = ont_provision_steps.create_service_port(
        ctx.db,
        ctx.ont_id,
        vlan_id=vlan_id,
        gem_index=ctx.step_data.get("gem_index", 1),
        user_vlan=ctx.step_data.get("user_vlan"),
        tag_transform=ctx.step_data.get("tag_transform", "translate"),
        idempotent=True,
    )

    # Store created port info for potential rollback
    if result.success and result.data:
        ctx.step_data["created_service_port"] = result.data

    return result


def _compensate_service_ports(ctx: SagaContext, original: StepResult) -> StepResult:
    """Rollback service ports created by _create_service_ports."""
    from app.services.network import ont_provision_steps

    logger.info(
        "Compensating service ports for ONT %s",
        ctx.ont_id,
        extra={"event": "saga_compensate_service_ports"},
    )

    return ont_provision_steps.rollback_service_ports(ctx.db, ctx.ont_id)


def _configure_management_ip(ctx: SagaContext) -> StepResult:
    """Configure management IP (IPHOST) for TR-069 access.

    Uses step_data keys:
    - mgmt_vlan_id: Management VLAN (required)
    - mgmt_ip_mode: "dhcp" or "static" (default: "dhcp")
    - mgmt_ip_address: Static IP (if static mode)
    - mgmt_subnet: Subnet mask (if static mode)
    - mgmt_gateway: Gateway IP (if static mode)
    """
    from app.services.network import ont_provision_steps

    vlan_id = ctx.step_data.get("mgmt_vlan_id")
    if vlan_id is None:
        # Try to get from provisioning profile
        if ctx.ont is not None and ctx.ont.provisioning_profile_id:
            from app.services.network.ont_provisioning.profiles import resolve_profile

            profile = resolve_profile(ctx.db, ctx.ont)
            if profile and profile.mgmt_vlan_tag:
                vlan_id = profile.mgmt_vlan_tag
                ctx.step_data["mgmt_vlan_id"] = vlan_id

    if vlan_id is None:
        return StepResult(
            step_name="configure_management_ip",
            success=False,
            message="mgmt_vlan_id not provided and no profile default",
            critical=False,
        )

    return ont_provision_steps.configure_management_ip(
        ctx.db,
        ctx.ont_id,
        vlan_id=vlan_id,
        ip_mode=ctx.step_data.get("mgmt_ip_mode", "dhcp"),
        ip_address=ctx.step_data.get("mgmt_ip_address"),
        subnet=ctx.step_data.get("mgmt_subnet"),
        gateway=ctx.step_data.get("mgmt_gateway"),
    )


def _activate_internet_config(ctx: SagaContext) -> StepResult:
    """Activate TCP stack on ONT management WAN.

    Uses step_data keys:
    - internet_config_ip_index: IP index for internet-config (default: 0)
    """
    from app.services.network import ont_provision_steps

    return ont_provision_steps.activate_internet_config(
        ctx.db,
        ctx.ont_id,
        ip_index=ctx.step_data.get("internet_config_ip_index", 0),
    )


def _configure_wan_olt(ctx: SagaContext) -> StepResult:
    """Configure WAN route+NAT mode on OLT side.

    Uses step_data keys:
    - wan_ip_index: IP index for WAN config (default: 0)
    - wan_profile_id: OLT WAN profile ID (default: 0)
    """
    from app.services.network import ont_provision_steps

    return ont_provision_steps.configure_wan_olt(
        ctx.db,
        ctx.ont_id,
        ip_index=ctx.step_data.get("wan_ip_index", 0),
        profile_id=ctx.step_data.get("wan_profile_id", 0),
    )


def _bind_tr069(ctx: SagaContext) -> StepResult:
    """Bind TR-069 server profile to ONT.

    Uses step_data keys:
    - tr069_olt_profile_id: OLT-level TR-069 profile ID (required)
    """
    from app.services.network import ont_provision_steps

    profile_id = ctx.step_data.get("tr069_olt_profile_id")
    if profile_id is None:
        # Try to resolve from linked ACS server
        if ctx.olt is not None:
            from app.services.network.olt_tr069_admin import (
                ensure_tr069_profile_for_linked_acs,
            )

            ok, msg, resolved_id = ensure_tr069_profile_for_linked_acs(ctx.olt)
            if ok and resolved_id is not None:
                profile_id = resolved_id
                ctx.step_data["tr069_olt_profile_id"] = profile_id

    if profile_id is None:
        return StepResult(
            step_name="bind_tr069",
            success=False,
            message="tr069_olt_profile_id not available",
            critical=False,
        )

    return ont_provision_steps.bind_tr069(
        ctx.db,
        ctx.ont_id,
        tr069_olt_profile_id=profile_id,
    )


def _wait_tr069_bootstrap(ctx: SagaContext) -> StepResult:
    """Queue wait for ONT to appear in ACS.

    This step queues a background task to poll for ONT registration.
    It returns immediately with waiting=True.
    """
    from app.services.network import ont_provision_steps

    return ont_provision_steps.queue_wait_tr069_bootstrap(
        ctx.db,
        ctx.ont_id,
        initiated_by=ctx.initiated_by,
    )


def _apply_saved_service_config(ctx: SagaContext) -> StepResult:
    """Apply saved WAN/WiFi/LAN configuration via TR-069.

    This reads saved configuration from OntUnit and OntWanServiceInstance
    records and pushes them to the ONT via ACS.
    """
    from app.services.network import ont_provision_steps

    return ont_provision_steps.apply_saved_service_config(ctx.db, ctx.ont_id)


def _push_pppoe_tr069(ctx: SagaContext) -> StepResult:
    """Push PPPoE credentials via TR-069.

    Uses step_data keys:
    - pppoe_username: PPPoE username (required)
    - pppoe_password: PPPoE password (required)
    - pppoe_instance_index: WAN instance (default: 1)
    """
    from app.services.network import ont_provision_steps

    username = ctx.step_data.get("pppoe_username")
    password = ctx.step_data.get("pppoe_password")

    if not username or not password:
        # Try to get from ONT saved config
        if ctx.ont is not None:
            username = username or ctx.ont.pppoe_username
            password = password or ctx.ont.pppoe_password

    if not username or not password:
        return StepResult(
            step_name="push_pppoe_tr069",
            success=False,
            message="PPPoE credentials not available",
            critical=False,
        )

    return ont_provision_steps.push_pppoe_tr069(
        ctx.db,
        ctx.ont_id,
        username=username,
        password=password,
        instance_index=ctx.step_data.get("pppoe_instance_index", 1),
    )


def _configure_wifi(ctx: SagaContext) -> StepResult:
    """Configure WiFi via TR-069.

    Uses step_data keys:
    - wifi_ssid: WiFi SSID (optional, uses saved if not provided)
    - wifi_password: WiFi password (optional, uses saved if not provided)
    - wifi_enabled: Enable WiFi (default: True)
    - wifi_channel: WiFi channel (default: "auto")
    - wifi_security_mode: Security mode (default: "WPA2-PSK")
    """
    from app.services.acs_config_adapter import acs_config_adapter

    ssid = ctx.step_data.get("wifi_ssid")
    password = ctx.step_data.get("wifi_password")

    # Try to get from ONT saved config
    if ctx.ont is not None:
        ssid = ssid or ctx.ont.wifi_ssid
        password = password or ctx.ont.wifi_password

    if not ssid:
        return StepResult(
            step_name="configure_wifi",
            success=True,
            message="No WiFi SSID configured, skipping",
            skipped=True,
        )

    result = acs_config_adapter.set_wifi_config(
        ctx.db,
        ctx.ont_id,
        enabled=ctx.step_data.get("wifi_enabled", True),
        ssid=ssid,
        password=password,
        channel=ctx.step_data.get("wifi_channel", "auto"),
        security_mode=ctx.step_data.get("wifi_security_mode", "WPA2-PSK"),
    )

    return StepResult(
        step_name="configure_wifi",
        success=result.success if hasattr(result, "success") else True,
        message=result.message if hasattr(result, "message") else "WiFi configured",
        data=result.data if hasattr(result, "data") else None,
    )


def _configure_lan(ctx: SagaContext) -> StepResult:
    """Configure LAN via TR-069.

    Uses step_data keys:
    - lan_ip: LAN gateway IP (default: "192.168.1.1")
    - lan_subnet: LAN subnet mask (default: "255.255.255.0")
    - dhcp_enabled: Enable DHCP server (default: True)
    - dhcp_start: DHCP pool start (default: "192.168.1.100")
    - dhcp_end: DHCP pool end (default: "192.168.1.200")
    """
    from app.services.acs_config_adapter import acs_config_adapter

    result = acs_config_adapter.set_lan_config(
        ctx.db,
        ctx.ont_id,
        lan_ip=ctx.step_data.get("lan_ip", "192.168.1.1"),
        lan_subnet=ctx.step_data.get("lan_subnet", "255.255.255.0"),
        dhcp_enabled=ctx.step_data.get("dhcp_enabled", True),
        dhcp_start=ctx.step_data.get("dhcp_start", "192.168.1.100"),
        dhcp_end=ctx.step_data.get("dhcp_end", "192.168.1.200"),
    )

    return StepResult(
        step_name="configure_lan",
        success=result.success if hasattr(result, "success") else True,
        message=result.message if hasattr(result, "message") else "LAN configured",
        data=result.data if hasattr(result, "data") else None,
    )


def _provision_with_reconciliation(ctx: SagaContext) -> StepResult:
    """Full provisioning using state reconciliation.

    This is the recommended approach for comprehensive provisioning.
    It builds desired state, reads actual state, computes delta,
    validates, and executes with compensation on failure.

    Uses step_data keys:
    - profile_id: Provisioning profile ID (optional)
    - tr069_olt_profile_id: TR-069 profile ID (optional)
    - allow_low_optical_margin: Allow provisioning with low margin (default: False)
    """
    from app.services.network import ont_provision_steps

    return ont_provision_steps.provision_with_reconciliation(
        ctx.db,
        ctx.ont_id,
        profile_id=ctx.step_data.get("profile_id"),
        tr069_olt_profile_id=ctx.step_data.get("tr069_olt_profile_id"),
        dry_run=ctx.dry_run,
        allow_low_optical_margin=ctx.step_data.get("allow_low_optical_margin", False),
    )


# ---------------------------------------------------------------------------
# Saga Builders
# ---------------------------------------------------------------------------


def build_internet_provisioning_saga(
    *,
    internet_vlan_id: int,
    mgmt_vlan_id: int | None = None,
    tr069_olt_profile_id: int | None = None,
) -> SagaDefinition:
    """Build a saga for basic internet service provisioning.

    This saga performs:
    1. Create service ports for internet VLAN
    2. Configure management IP (optional)
    3. Bind TR-069 profile (optional)
    4. Queue wait for ACS discovery
    5. Apply saved service config

    Args:
        internet_vlan_id: VLAN ID for internet service.
        mgmt_vlan_id: Management VLAN for TR-069 (optional).
        tr069_olt_profile_id: OLT TR-069 profile ID (optional).

    Returns:
        SagaDefinition ready for execution.
    """
    steps = [
        SagaStep(
            name="create_service_ports",
            action=_create_service_ports,
            compensate=_compensate_service_ports,
            critical=True,
            description="Create internet service ports on OLT",
        ),
    ]

    if mgmt_vlan_id is not None:
        steps.append(
            SagaStep(
                name="configure_management_ip",
                action=_configure_management_ip,
                compensate=None,  # Management IP removal is safe
                critical=False,
                description="Configure management IP for TR-069",
            )
        )

    if tr069_olt_profile_id is not None:
        steps.append(
            SagaStep(
                name="bind_tr069",
                action=_bind_tr069,
                compensate=None,  # TR-069 rebind is idempotent
                critical=False,
                description="Bind TR-069 server profile",
            )
        )

    steps.extend([
        SagaStep(
            name="wait_tr069_bootstrap",
            action=_wait_tr069_bootstrap,
            compensate=None,
            critical=False,
            description="Queue wait for ACS discovery",
        ),
        SagaStep(
            name="apply_saved_service_config",
            action=_apply_saved_service_config,
            compensate=None,
            critical=False,
            description="Apply saved WAN/WiFi/LAN config",
        ),
    ])

    return SagaDefinition(
        name="internet_provisioning",
        description="Basic internet service provisioning with OLT and ACS config",
        steps=steps,
        version="1.0",
    )


# ---------------------------------------------------------------------------
# Pre-built Saga Definitions
# ---------------------------------------------------------------------------


WIFI_SETUP_SAGA = SagaDefinition(
    name="wifi_setup",
    description="WiFi-only configuration via TR-069",
    steps=[
        SagaStep(
            name="configure_wifi",
            action=_configure_wifi,
            compensate=None,  # WiFi config is easily re-pushed
            critical=False,
            description="Configure WiFi SSID and password",
        ),
    ],
    version="1.0",
)


FULL_PROVISIONING_SAGA = SagaDefinition(
    name="full_provisioning",
    description="Complete ONT provisioning with OLT config, TR-069 binding, and service config",
    steps=[
        SagaStep(
            name="provision_with_reconciliation",
            action=_provision_with_reconciliation,
            compensate=_compensate_service_ports,  # Rollback via service port deletion
            critical=True,
            description="Full provisioning via state reconciliation",
        ),
        SagaStep(
            name="wait_tr069_bootstrap",
            action=_wait_tr069_bootstrap,
            compensate=None,
            critical=False,
            description="Queue wait for ACS discovery",
        ),
        SagaStep(
            name="apply_saved_service_config",
            action=_apply_saved_service_config,
            compensate=None,
            critical=False,
            description="Apply saved WAN/WiFi/LAN config",
        ),
    ],
    version="1.0",
)


ACS_CONFIG_SAGA = SagaDefinition(
    name="acs_config",
    description="ACS-side configuration (WiFi, LAN, PPPoE)",
    steps=[
        SagaStep(
            name="push_pppoe_tr069",
            action=_push_pppoe_tr069,
            compensate=None,
            critical=False,
            description="Push PPPoE credentials via TR-069",
        ),
        SagaStep(
            name="configure_wifi",
            action=_configure_wifi,
            compensate=None,
            critical=False,
            description="Configure WiFi",
        ),
        SagaStep(
            name="configure_lan",
            action=_configure_lan,
            compensate=None,
            critical=False,
            description="Configure LAN/DHCP",
        ),
    ],
    version="1.0",
)


# Saga registry for lookup by name
SAGA_REGISTRY: dict[str, SagaDefinition] = {
    "wifi_setup": WIFI_SETUP_SAGA,
    "full_provisioning": FULL_PROVISIONING_SAGA,
    "acs_config": ACS_CONFIG_SAGA,
}


def get_saga_by_name(name: str) -> SagaDefinition | None:
    """Get a saga definition by name.

    Args:
        name: Saga name from registry.

    Returns:
        SagaDefinition or None if not found.
    """
    return SAGA_REGISTRY.get(name)


def list_available_sagas() -> list[dict]:
    """List all available saga definitions.

    Returns:
        List of saga info dictionaries.
    """
    return [
        {
            "name": saga.name,
            "description": saga.description,
            "version": saga.version,
            "steps": [s.name for s in saga.steps],
        }
        for saga in SAGA_REGISTRY.values()
    ]
