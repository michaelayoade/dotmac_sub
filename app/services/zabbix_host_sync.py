"""Sync OLTDevice/NasDevice to Zabbix hosts.

This module provides bidirectional synchronization between DotMac network
devices and Zabbix monitoring hosts. It creates/updates Zabbix hosts
for OLTs and NAS devices, maintaining consistent monitoring coverage.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice, NasDeviceStatus
from app.models.network import DeviceStatus, OLTDevice
from app.services.zabbix import ZabbixClient, ZabbixClientError

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Host group names in Zabbix
ZABBIX_GROUP_OLT = "DotMac/Network/OLT"
ZABBIX_GROUP_NAS = "DotMac/Network/NAS"
ZABBIX_GROUP_INFRASTRUCTURE = "DotMac/Infrastructure"

# Template names (must exist in Zabbix)
ZABBIX_TEMPLATE_OLT_SNMP = "DotMac OLT GPON"
ZABBIX_TEMPLATE_NAS_SNMP = "DotMac MikroTik NAS"


class ZabbixHostSyncResult:
    """Result of a sync operation."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.updated: list[str] = []
        self.failed: list[tuple[str, str]] = []  # (name, error)
        self.skipped: list[str] = []

    def __repr__(self) -> str:
        return (
            f"ZabbixHostSyncResult("
            f"created={len(self.created)}, "
            f"updated={len(self.updated)}, "
            f"failed={len(self.failed)}, "
            f"skipped={len(self.skipped)})"
        )

    def to_dict(self) -> dict[str, int | list]:
        return {
            "created": len(self.created),
            "updated": len(self.updated),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "created_names": self.created,
            "updated_names": self.updated,
            "failed_items": self.failed,
            "skipped_names": self.skipped,
        }


def _get_client() -> ZabbixClient:
    """Get Zabbix client from environment."""
    return ZabbixClient.from_env()


def _get_template_id(client: ZabbixClient, template_name: str) -> str | None:
    """Get template ID by name, or None if not found."""
    try:
        templates = client.get_templates(name=template_name)
        if templates:
            return templates[0]["templateid"]
    except ZabbixClientError:
        pass
    return None


def _build_olt_tags(olt: OLTDevice) -> list[dict[str, str]]:
    """Build Zabbix host tags from OLT metadata."""
    tags = [
        {"tag": "device_type", "value": "olt"},
        {"tag": "dotmac_id", "value": str(olt.id)},
    ]
    if olt.vendor:
        tags.append({"tag": "vendor", "value": olt.vendor})
    if olt.model:
        tags.append({"tag": "model", "value": olt.model})
    return tags


def _build_olt_inventory(olt: OLTDevice) -> dict[str, str]:
    """Build Zabbix host inventory from OLT metadata."""
    inventory: dict[str, str] = {}
    if olt.serial_number:
        inventory["serialno_a"] = olt.serial_number
    if olt.model:
        inventory["model"] = olt.model
    if olt.vendor:
        inventory["vendor"] = olt.vendor
    if olt.firmware_version:
        inventory["os_full"] = olt.firmware_version
    return inventory


def _build_nas_tags(nas: NasDevice) -> list[dict[str, str]]:
    """Build Zabbix host tags from NAS metadata."""
    tags = [
        {"tag": "device_type", "value": "nas"},
        {"tag": "dotmac_id", "value": str(nas.id)},
    ]
    if nas.vendor:
        tags.append({"tag": "vendor", "value": nas.vendor.value})
    if nas.model:
        tags.append({"tag": "model", "value": nas.model})
    return tags


def _build_nas_inventory(nas: NasDevice) -> dict[str, str]:
    """Build Zabbix host inventory from NAS metadata."""
    inventory: dict[str, str] = {}
    if nas.serial_number:
        inventory["serialno_a"] = nas.serial_number
    if nas.model:
        inventory["model"] = nas.model
    if nas.vendor:
        inventory["vendor"] = nas.vendor.value
    if nas.firmware_version:
        inventory["os_full"] = nas.firmware_version
    return inventory


def sync_olt_to_zabbix(
    db: Session,
    olt: OLTDevice,
    client: ZabbixClient | None = None,
) -> str | None:
    """Sync a single OLT to Zabbix.

    Creates or updates a Zabbix host for the OLT.
    Returns the Zabbix host ID, or None if sync failed.
    """
    if client is None:
        client = _get_client()

    # Skip inactive OLTs
    if olt.status != DeviceStatus.active or not olt.is_active:
        logger.debug("skip_olt_inactive", extra={"olt_id": str(olt.id)})
        return None

    # Must have management IP
    if not olt.mgmt_ip:
        logger.warning("skip_olt_no_ip", extra={"olt_id": str(olt.id)})
        return None

    host_name = f"olt-{olt.hostname or olt.name}".lower().replace(" ", "-")
    display_name = f"OLT: {olt.name}"

    try:
        # Get or create host group
        group_id = client.get_or_create_host_group(ZABBIX_GROUP_OLT)

        # Get template ID (optional)
        template_id = _get_template_id(client, ZABBIX_TEMPLATE_OLT_SNMP)
        template_ids = [template_id] if template_id else None

        tags = _build_olt_tags(olt)
        inventory = _build_olt_inventory(olt)

        if olt.zabbix_host_id:
            # Update existing host
            client.update_host(
                host_id=olt.zabbix_host_id,
                name=display_name,
                group_ids=[group_id],
                template_ids=template_ids,
                tags=tags,
                inventory=inventory,
            )
            logger.info(
                "zabbix_olt_updated",
                extra={"olt_id": str(olt.id), "zabbix_host_id": olt.zabbix_host_id},
            )
        else:
            # Create new host
            zabbix_host_id = client.create_host(
                host=host_name,
                name=display_name,
                group_ids=[group_id],
                template_ids=template_ids,
                interface_ip=olt.mgmt_ip,
                interface_type=2,  # SNMP
                tags=tags,
                inventory=inventory,
            )
            olt.zabbix_host_id = zabbix_host_id
            logger.info(
                "zabbix_olt_created",
                extra={"olt_id": str(olt.id), "zabbix_host_id": zabbix_host_id},
            )

        olt.zabbix_last_sync_at = datetime.now(UTC)
        db.flush()
        return olt.zabbix_host_id

    except ZabbixClientError as exc:
        logger.error(
            "zabbix_olt_sync_failed",
            extra={"olt_id": str(olt.id), "error": str(exc)},
        )
        return None


def sync_nas_to_zabbix(
    db: Session,
    nas: NasDevice,
    client: ZabbixClient | None = None,
) -> str | None:
    """Sync a single NAS to Zabbix.

    Creates or updates a Zabbix host for the NAS.
    Returns the Zabbix host ID, or None if sync failed.
    """
    if client is None:
        client = _get_client()

    # Skip inactive NAS devices
    if nas.status != NasDeviceStatus.active or not nas.is_active:
        logger.debug("skip_nas_inactive", extra={"nas_id": str(nas.id)})
        return None

    # Must have management IP
    mgmt_ip = nas.management_ip or nas.ip_address
    if not mgmt_ip:
        logger.warning("skip_nas_no_ip", extra={"nas_id": str(nas.id)})
        return None

    host_name = f"nas-{nas.code or nas.name}".lower().replace(" ", "-")
    display_name = f"NAS: {nas.name}"

    try:
        # Get or create host group
        group_id = client.get_or_create_host_group(ZABBIX_GROUP_NAS)

        # Get template ID (optional)
        template_id = _get_template_id(client, ZABBIX_TEMPLATE_NAS_SNMP)
        template_ids = [template_id] if template_id else None

        tags = _build_nas_tags(nas)
        inventory = _build_nas_inventory(nas)

        if nas.zabbix_host_id:
            # Update existing host
            client.update_host(
                host_id=nas.zabbix_host_id,
                name=display_name,
                group_ids=[group_id],
                template_ids=template_ids,
                tags=tags,
                inventory=inventory,
            )
            logger.info(
                "zabbix_nas_updated",
                extra={"nas_id": str(nas.id), "zabbix_host_id": nas.zabbix_host_id},
            )
        else:
            # Create new host
            zabbix_host_id = client.create_host(
                host=host_name,
                name=display_name,
                group_ids=[group_id],
                template_ids=template_ids,
                interface_ip=mgmt_ip,
                interface_type=2,  # SNMP
                tags=tags,
                inventory=inventory,
            )
            nas.zabbix_host_id = zabbix_host_id
            logger.info(
                "zabbix_nas_created",
                extra={"nas_id": str(nas.id), "zabbix_host_id": zabbix_host_id},
            )

        nas.zabbix_last_sync_at = datetime.now(UTC)
        db.flush()
        return nas.zabbix_host_id

    except ZabbixClientError as exc:
        logger.error(
            "zabbix_nas_sync_failed",
            extra={"nas_id": str(nas.id), "error": str(exc)},
        )
        return None


def sync_all_olts(db: Session) -> ZabbixHostSyncResult:
    """Sync all active OLTs to Zabbix."""
    result = ZabbixHostSyncResult()
    client = _get_client()

    stmt = select(OLTDevice).where(
        OLTDevice.is_active.is_(True),
        OLTDevice.status == DeviceStatus.active,
    )
    olts = db.scalars(stmt).all()

    for olt in olts:
        if not olt.mgmt_ip:
            result.skipped.append(olt.name)
            continue

        try:
            had_id = bool(olt.zabbix_host_id)
            host_id = sync_olt_to_zabbix(db, olt, client=client)

            if host_id:
                if had_id:
                    result.updated.append(olt.name)
                else:
                    result.created.append(olt.name)
            else:
                result.skipped.append(olt.name)
        except Exception as exc:
            result.failed.append((olt.name, str(exc)))
            logger.exception("olt_sync_exception", extra={"olt_id": str(olt.id)})

    return result


def sync_all_nas_devices(db: Session) -> ZabbixHostSyncResult:
    """Sync all active NAS devices to Zabbix."""
    result = ZabbixHostSyncResult()
    client = _get_client()

    stmt = select(NasDevice).where(
        NasDevice.is_active.is_(True),
        NasDevice.status == NasDeviceStatus.active,
    )
    devices = db.scalars(stmt).all()

    for nas in devices:
        mgmt_ip = nas.management_ip or nas.ip_address
        if not mgmt_ip:
            result.skipped.append(nas.name)
            continue

        try:
            had_id = bool(nas.zabbix_host_id)
            host_id = sync_nas_to_zabbix(db, nas, client=client)

            if host_id:
                if had_id:
                    result.updated.append(nas.name)
                else:
                    result.created.append(nas.name)
            else:
                result.skipped.append(nas.name)
        except Exception as exc:
            result.failed.append((nas.name, str(exc)))
            logger.exception("nas_sync_exception", extra={"nas_id": str(nas.id)})

    return result


def sync_all_devices(db: Session) -> dict[str, dict]:
    """Sync all OLTs and NAS devices to Zabbix.

    Returns a summary of sync results for both device types.
    """
    olt_result = sync_all_olts(db)
    nas_result = sync_all_nas_devices(db)

    logger.info(
        "zabbix_full_sync_complete",
        extra={
            "olt_created": len(olt_result.created),
            "olt_updated": len(olt_result.updated),
            "olt_failed": len(olt_result.failed),
            "nas_created": len(nas_result.created),
            "nas_updated": len(nas_result.updated),
            "nas_failed": len(nas_result.failed),
        },
    )

    return {
        "olt": olt_result.to_dict(),
        "nas": nas_result.to_dict(),
    }


def remove_device_from_zabbix(
    db: Session,
    device_type: str,
    device_id: UUID,
) -> bool:
    """Remove a device's Zabbix host when decommissioned.

    Args:
        device_type: "olt" or "nas"
        device_id: UUID of the device
    """
    client = _get_client()

    if device_type == "olt":
        olt = db.get(OLTDevice, device_id)
        if not olt or not olt.zabbix_host_id:
            return False
        try:
            client.delete_host(olt.zabbix_host_id)
            olt.zabbix_host_id = None
            olt.zabbix_last_sync_at = None
            db.flush()
            logger.info(
                "zabbix_olt_removed",
                extra={"olt_id": str(device_id)},
            )
            return True
        except ZabbixClientError as exc:
            logger.error(
                "zabbix_olt_remove_failed",
                extra={"olt_id": str(device_id), "error": str(exc)},
            )
            return False

    elif device_type == "nas":
        nas = db.get(NasDevice, device_id)
        if not nas or not nas.zabbix_host_id:
            return False
        try:
            client.delete_host(nas.zabbix_host_id)
            nas.zabbix_host_id = None
            nas.zabbix_last_sync_at = None
            db.flush()
            logger.info(
                "zabbix_nas_removed",
                extra={"nas_id": str(device_id)},
            )
            return True
        except ZabbixClientError as exc:
            logger.error(
                "zabbix_nas_remove_failed",
                extra={"nas_id": str(device_id), "error": str(exc)},
            )
            return False

    return False
