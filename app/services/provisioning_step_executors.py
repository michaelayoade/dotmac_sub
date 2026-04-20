"""Step executors for automated fiber provisioning.

Implements the execution logic for provisioning steps that automate
OLT service-port creation, NAS VLAN configuration, and TR-069
credential push during the service order workflow.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.services.provisioning_adapters import ProvisioningResult

logger = logging.getLogger(__name__)


def execute_create_olt_service_port(
    db: Session,
    context: dict[str, Any],
    config: dict[str, Any] | None,
) -> ProvisioningResult:
    """Create an OLT service-port mapping ONT GEM to VLAN.

    Resolves ONT assignment from context, determines VLAN from config
    or provisioning profile, and creates the service-port via OLT SSH.

    Context keys used:
        ont_unit_id: ONT unit ID
        subscription_id: Subscription ID (to resolve ONT assignment)

    Config keys:
        vlan_id: VLAN ID (required if not resolvable from profile)
        gem_index: GEM port index (default: 1)
    """
    config = config or {}
    ont_unit_id = context.get("ont_unit_id")
    subscription_id = context.get("subscription_id")
    subscriber_id = context.get("subscriber_id")

    # Use Subscriber-ONT adapter to resolve complete provisioning context
    if not ont_unit_id and (subscription_id or subscriber_id):
        from app.services.network.subscriber_ont_adapter import (
            resolve_provisioning_context,
        )

        prov_context = resolve_provisioning_context(
            db,
            subscriber_id=subscriber_id,
            subscription_id=subscription_id,
        )
        ont_unit_id = prov_context.ont_id

    if not ont_unit_id:
        return ProvisioningResult(
            status="failed",
            detail="No ONT unit ID found in context or subscriber assignments.",
        )

    vlan_id = config.get("vlan_id")
    gem_index = int(config.get("gem_index", 1))

    if not vlan_id:
        return ProvisioningResult(
            status="failed",
            detail="VLAN ID is required in step config for OLT service-port creation.",
        )

    try:
        from app.services.network.olt_protocol_adapters import get_protocol_adapter
        from app.services.web_network_service_ports import _resolve_ont_olt_context

        ont_ctx = _resolve_ont_olt_context(db, ont_unit_id)
        if isinstance(ont_ctx, tuple):
            ont, olt, fsp, olt_ont_id = ont_ctx
        elif isinstance(ont_ctx, dict):
            ont_ctx.get("ont")
            olt = ont_ctx.get("olt")
            fsp = ont_ctx.get("fsp")
            olt_ont_id = ont_ctx.get("olt_ont_id")
        else:
            olt = fsp = olt_ont_id = None
        if olt is None or fsp is None or olt_ont_id is None:
            return ProvisioningResult(
                status="failed",
                detail="Could not resolve ONT/OLT context for service-port creation.",
            )

        adapter = get_protocol_adapter(olt)
        result = adapter.create_service_port(
            fsp,
            olt_ont_id,
            gem_index=gem_index,
            vlan_id=int(vlan_id),
        )
        success = result.success
        message = result.message

        if success:
            logger.info(
                "OLT service-port created: ONT %s, VLAN %s, GEM %d",
                ont_unit_id,
                vlan_id,
                gem_index,
            )
            return ProvisioningResult(
                status="ok",
                detail=message,
                payload={"olt_service_port_created": True, "vlan_id": vlan_id},
            )
        return ProvisioningResult(status="failed", detail=message)
    except Exception as exc:
        logger.error("OLT service-port creation failed: %s", exc)
        return ProvisioningResult(
            status="failed", detail=f"OLT service-port creation failed: {exc}"
        )


def execute_ensure_nas_vlan(
    db: Session,
    context: dict[str, Any],
    config: dict[str, Any] | None,
) -> ProvisioningResult:
    """Ensure NAS VLAN interface + IP + PPPoE server exist.

    Uses MikroTik RouterOS API to idempotently create the VLAN chain.

    Context keys used:
        nas_device_id: NAS device ID
        subscription_id: to resolve NAS from subscription

    Config keys:
        vlan_id: VLAN ID (required)
        parent_interface: Physical interface (default: 'ether3')
        ip_address: IP address with CIDR (required)
        pppoe_service_name: Optional PPPoE service name
        pppoe_default_profile: PPP profile (default: 'default')
    """
    config = config or {}
    nas_device_id = config.get("nas_device_id") or context.get("nas_device_id")

    if not nas_device_id:
        # Try to resolve from subscription
        subscription_id = context.get("subscription_id")
        if subscription_id:
            from app.models.catalog import Subscription

            sub = db.get(Subscription, subscription_id)
            if sub and sub.provisioning_nas_device_id:
                nas_device_id = str(sub.provisioning_nas_device_id)

    if not nas_device_id:
        return ProvisioningResult(
            status="failed",
            detail="No NAS device ID found in context or subscription.",
        )

    vlan_id = config.get("vlan_id")
    parent_interface = config.get("parent_interface", "ether3")
    ip_address = config.get("ip_address")

    if not vlan_id:
        return ProvisioningResult(
            status="failed", detail="VLAN ID is required in step config."
        )
    if not ip_address:
        return ProvisioningResult(
            status="failed", detail="IP address is required in step config."
        )

    try:
        from app.models.catalog import NasDevice

        nas = db.get(NasDevice, nas_device_id)
        if not nas:
            return ProvisioningResult(
                status="failed", detail=f"NAS device {nas_device_id} not found."
            )

        from app.services.nas._mikrotik_vlan import provision_vlan_full

        result = provision_vlan_full(
            nas,
            vlan_id=int(vlan_id),
            parent_interface=parent_interface,
            ip_address=ip_address,
            pppoe_service_name=config.get("pppoe_service_name"),
            pppoe_default_profile=config.get("pppoe_default_profile", "default"),
        )

        if result.success:
            return ProvisioningResult(
                status="ok",
                detail=result.message,
                payload={
                    "nas_vlan_provisioned": True,
                    "vlan_id": vlan_id,
                    "nas_device_id": nas_device_id,
                },
            )
        return ProvisioningResult(status="failed", detail=result.message)
    except Exception as exc:
        logger.error("NAS VLAN provisioning failed: %s", exc)
        return ProvisioningResult(
            status="failed", detail=f"NAS VLAN provisioning failed: {exc}"
        )


def execute_push_tr069_wan_config(
    db: Session,
    context: dict[str, Any],
    config: dict[str, Any] | None,
) -> ProvisioningResult:
    """Push WAN configuration to ONT/CPE via TR-069.

    Sets WAN connection type and VLAN on the device via GenieACS.

    Context keys used:
        ont_unit_id: ONT unit ID (for ONT-based push)
        cpe_device_id: CPE device ID (for CPE-based push)

    Config keys:
        wan_mode: WAN mode (pppoe, dhcp, bridge). Default: pppoe
        wan_vlan: WAN VLAN ID (optional)
    """
    config = config or {}
    ont_unit_id = context.get("ont_unit_id")
    cpe_device_id = context.get("cpe_device_id")
    wan_mode = config.get("wan_mode", "pppoe")

    try:
        if ont_unit_id:
            from app.services.network.ont_action_common import (
                detect_data_model_root,
                get_ont_client_or_error,
            )

            ont_resolved, ont_error = get_ont_client_or_error(db, ont_unit_id)
            if ont_error:
                return ProvisioningResult(status="failed", detail=ont_error.message)
            if ont_resolved is None:
                return ProvisioningResult(
                    status="failed", detail="ONT resolution failed."
                )
            ont, client, device_id = ont_resolved
            root = detect_data_model_root(db, ont, client, device_id)
        elif cpe_device_id:
            from app.services.network.ont_action_common import (
                detect_data_model_root,
                get_cpe_client_or_error,
            )

            cpe_resolved, cpe_error = get_cpe_client_or_error(db, cpe_device_id)
            if cpe_error:
                return ProvisioningResult(status="failed", detail=cpe_error.message)
            if cpe_resolved is None:
                return ProvisioningResult(
                    status="failed", detail="CPE resolution failed."
                )
            cpe, client, device_id = cpe_resolved
            root = detect_data_model_root(db, cpe, client, device_id)
        else:
            return ProvisioningResult(
                status="failed",
                detail="No ONT or CPE device ID in context for TR-069 WAN config push.",
            )

        # Build WAN parameters based on mode
        from app.services.genieacs import GenieACSError
        from app.services.network.ont_action_common import (
            build_tr069_params,
            set_and_verify,
        )

        params: dict[str, str] = {}
        if wan_mode == "pppoe":
            if root == "Device":
                params["PPP.Interface.1.Enable"] = "true"
            else:
                params[
                    "WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Enable"
                ] = "1"
                params[
                    "WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ConnectionType"
                ] = "IP_Routed"

        wan_vlan = config.get("wan_vlan")
        if wan_vlan:
            if root == "Device":
                params["Ethernet.VLANTermination.1.VLANID"] = str(wan_vlan)
            else:
                params[
                    "WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.X_HW_VLAN"
                ] = str(wan_vlan)

        if params:
            tr069_params = build_tr069_params(root, params)
            set_and_verify(client, device_id, tr069_params)

        logger.info("TR-069 WAN config pushed: mode=%s, vlan=%s", wan_mode, wan_vlan)
        return ProvisioningResult(
            status="ok",
            detail=f"WAN config pushed via TR-069 (mode={wan_mode}).",
            payload={"tr069_wan_configured": True, "wan_mode": wan_mode},
        )
    except GenieACSError as exc:
        logger.error("TR-069 WAN config push failed: %s", exc)
        return ProvisioningResult(
            status="failed", detail=f"TR-069 WAN config push failed: {exc}"
        )
    except Exception as exc:
        logger.error("TR-069 WAN config push failed: %s", exc)
        return ProvisioningResult(
            status="failed", detail=f"TR-069 WAN config push failed: {exc}"
        )


def execute_push_tr069_pppoe_credentials(
    db: Session,
    context: dict[str, Any],
    config: dict[str, Any] | None,
) -> ProvisioningResult:
    """Push PPPoE credentials to ONT/CPE via TR-069.

    Resolves credentials from subscription's access credentials
    or from config overrides.

    Context keys used:
        ont_unit_id or cpe_device_id: Device to push to
        subscription_id: To resolve PPPoE credentials

    Config keys:
        pppoe_username: Override username (optional)
        pppoe_password: Override password (optional)
    """
    config = config or {}
    ont_unit_id = context.get("ont_unit_id")
    cpe_device_id = context.get("cpe_device_id")

    # Resolve credentials
    username = config.get("pppoe_username")
    password = config.get("pppoe_password")

    if not username or not password:
        subscription_id = context.get("subscription_id")
        if subscription_id:
            from sqlalchemy import select as sa_select

            from app.models.catalog import AccessCredential
            from app.services.credential_crypto import decrypt_credential

            cred: AccessCredential | None = None
            subscriber_id = context.get("subscriber_id")
            if subscriber_id:
                cred = db.scalars(
                    sa_select(AccessCredential).where(
                        AccessCredential.subscriber_id == subscriber_id,
                        AccessCredential.is_active.is_(True),
                    )
                ).first()
            if cred:
                username = username or cred.username
                password = password or decrypt_credential(cred.secret_hash)

    if not username or not password:
        return ProvisioningResult(
            status="failed",
            detail="PPPoE credentials not found in config or subscription.",
        )

    try:
        if ont_unit_id:
            from app.services.acs_client import create_acs_config_writer

            result = create_acs_config_writer().set_pppoe_credentials(
                db, ont_unit_id, username, password
            )
        elif cpe_device_id:
            # CPE doesn't handle PPPoE — it's behind the ONT. Skip gracefully.
            return ProvisioningResult(
                status="ok",
                detail="CPE devices don't handle PPPoE — credentials pushed to ONT only.",
                payload={"tr069_pppoe_skipped_cpe": True},
            )
        else:
            return ProvisioningResult(
                status="failed",
                detail="No ONT or CPE device ID for TR-069 PPPoE push.",
            )

        if result.success:
            masked = username[:3] + "***" if len(username) > 3 else "***"
            logger.info("PPPoE credentials pushed via TR-069 for user %s", masked)
            return ProvisioningResult(
                status="ok",
                detail=result.message,
                payload={"tr069_pppoe_pushed": True},
            )
        return ProvisioningResult(status="failed", detail=result.message)
    except Exception as exc:
        logger.error("TR-069 PPPoE push failed: %s", exc)
        return ProvisioningResult(
            status="failed", detail=f"TR-069 PPPoE push failed: {exc}"
        )
