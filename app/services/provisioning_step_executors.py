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


def execute_restore_olt_from_backup(
    db: Session,
    context: dict[str, Any],
    config: dict[str, Any] | None,
) -> ProvisioningResult:
    """Restore an OLT configuration backup through the provisioning workflow."""
    config = config or {}
    olt_id = config.get("olt_id") or context.get("olt_id")
    backup_id = config.get("backup_id") or context.get("backup_id")

    if not olt_id:
        return ProvisioningResult(
            status="failed",
            detail="OLT ID is required in step config or context.",
        )
    if not backup_id:
        return ProvisioningResult(
            status="failed",
            detail="Backup ID is required in step config or context.",
        )

    try:
        from app.services.network.olt_operations import restore_from_backup

        ok, message = restore_from_backup(db, str(olt_id), str(backup_id))
        if ok:
            return ProvisioningResult(
                status="ok",
                detail=message,
                payload={
                    "olt_backup_restored": True,
                    "olt_id": str(olt_id),
                    "backup_id": str(backup_id),
                },
            )
        return ProvisioningResult(status="failed", detail=message)
    except Exception as exc:
        logger.error("OLT backup restore failed: %s", exc)
        return ProvisioningResult(
            status="failed",
            detail=f"OLT backup restore failed: {exc}",
        )


def execute_push_tr069_wan_config(
    db: Session,
    context: dict[str, Any],
    config: dict[str, Any] | None,
) -> ProvisioningResult:
    """Legacy flat WAN TR-069 executor disabled after bundle-instance cutover."""
    return ProvisioningResult(
        status="failed",
        detail=(
            "Legacy TR-069 WAN config executor is disabled. Provision the active "
            "ONT WAN service instances instead."
        ),
    )


def execute_push_tr069_pppoe_credentials(
    db: Session,
    context: dict[str, Any],
    config: dict[str, Any] | None,
) -> ProvisioningResult:
    """Legacy flat PPPoE executor disabled after bundle-instance cutover."""
    return ProvisioningResult(
        status="failed",
        detail=(
            "Legacy TR-069 PPPoE credential executor is disabled. Provision the "
            "active ONT WAN service instances instead."
        ),
    )
