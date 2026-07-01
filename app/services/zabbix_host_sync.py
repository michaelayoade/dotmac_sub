"""Sync OLTDevice/NasDevice to Zabbix hosts.

This module provides bidirectional synchronization between DotMac network
devices and Zabbix monitoring hosts. It creates/updates Zabbix hosts
for OLTs and NAS devices, maintaining consistent monitoring coverage.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice, NasDeviceStatus
from app.models.network import DeviceStatus, OLTDevice
from app.services.credential_crypto import decrypt_credential
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

_HOST_KEY_UNSAFE = re.compile(r"[^a-z0-9._-]+")
_HOST_KEY_DASHES = re.compile(r"-+")


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


def _zabbix_host_key(prefix: str, value: str | None) -> str:
    """Return a Zabbix-safe technical host key."""
    normalized = str(value or "").strip().lower()
    normalized = _HOST_KEY_UNSAFE.sub("-", normalized)
    normalized = _HOST_KEY_DASHES.sub("-", normalized).strip("-._")
    return f"{prefix}-{normalized or 'device'}"


# Substrings Zabbix returns when an operation references a host id that no
# longer exists (deleted out-of-band). Used to recover from a stale
# ``zabbix_host_id`` by recreating the host instead of failing every cycle.
_MISSING_HOST_MARKERS = (
    "does not exist",
    "no permissions to referred object",
)


def _is_missing_host_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _MISSING_HOST_MARKERS)


def _find_adoptable_host_id(
    client: ZabbixClient, dotmac_id: str, interface_ip: str | None = None
) -> str | None:
    """Return the Zabbix host id already tagged with this device, if unambiguous.

    Hosts we create always carry a ``dotmac_id`` tag, so this recovers the id
    when the device lost it (or pre-dates the column) without creating a
    duplicate. If no tagged host exists, an exact unique interface-IP match is
    also adoptable for pre-existing manually-created hosts. Zero matches means
    nothing to adopt; more than one is ambiguous, so we decline to guess.
    """
    try:
        hosts = client.get_hosts_by_tag("dotmac_id", dotmac_id, limit=2)
    except ZabbixClientError:
        hosts = []
    if len(hosts) != 1:
        if not interface_ip:
            return None
        try:
            hosts = client.get_hosts_by_interface_ip(interface_ip, limit=2)
        except ZabbixClientError:
            return None
        if len(hosts) != 1:
            return None
    host_id = hosts[0].get("hostid")
    return str(host_id) if host_id else None


def _create_or_update_host(
    client: ZabbixClient,
    *,
    dotmac_id: str,
    stored_host_id: str | None,
    host_name: str,
    display_name: str,
    group_id: str,
    template_ids: list[str] | None,
    interface_ip: str,
    tags: list[dict[str, str]],
    inventory: dict[str, str],
    log_prefix: str,
) -> str:
    """Create or update a Zabbix host, recovering from id/name drift.

    Resolution order:
    - stored id present -> update it;
    - no stored id but a host already carries our ``dotmac_id`` tag -> adopt and
      update it (avoids a duplicate-name create that would fail every cycle);
    - otherwise create a new host.

    If an update targets a host that was deleted in Zabbix, the stale id is
    dropped and the host is recreated. Returns the resulting host id; raises
    ``ZabbixClientError`` on genuine failures.
    """
    host_id = stored_host_id
    if not host_id:
        host_id = _find_adoptable_host_id(client, dotmac_id, interface_ip)
        if host_id:
            logger.info(
                f"zabbix_{log_prefix}_adopted",
                extra={"dotmac_id": dotmac_id, "zabbix_host_id": host_id},
            )

    if host_id:
        try:
            # status=0 re-enables a host a previous deactivation had disabled, so
            # reactivating a device restores monitoring.
            client.update_host(
                host_id=host_id,
                name=display_name,
                group_ids=[group_id],
                template_ids=template_ids,
                tags=tags,
                inventory=inventory,
                status=0,
            )
            logger.info(
                f"zabbix_{log_prefix}_updated",
                extra={"dotmac_id": dotmac_id, "zabbix_host_id": host_id},
            )
            return host_id
        except ZabbixClientError as exc:
            if not _is_missing_host_error(exc):
                raise
            # The host was deleted out-of-band; drop the stale id and recreate.
            logger.warning(
                f"zabbix_{log_prefix}_host_missing_recreate",
                extra={"dotmac_id": dotmac_id, "zabbix_host_id": host_id},
            )

    host_id = client.create_host(
        host=host_name,
        name=display_name,
        group_ids=[group_id],
        template_ids=template_ids,
        interface_ip=interface_ip,
        interface_type=2,  # SNMP
        tags=tags,
        inventory=inventory,
    )
    logger.info(
        f"zabbix_{log_prefix}_created",
        extra={"dotmac_id": dotmac_id, "zabbix_host_id": host_id},
    )
    return host_id


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


def _zabbix_snmp_version(value: str | None) -> str:
    normalized = str(value or "v2c").strip().lower()
    if normalized in {"1", "v1", "snmpv1"}:
        return "1"
    if normalized in {"3", "v3", "snmpv3"}:
        return "3"
    return "2"


def _sync_olt_snmp_interface(client: ZabbixClient, olt: OLTDevice) -> bool:
    """Update the existing Zabbix SNMP interface to match OLT SNMP settings."""
    host_id = str(getattr(olt, "zabbix_host_id", "") or "").strip()
    if not host_id:
        return False

    hosts = client.get_hosts(host_id=host_id)
    host = hosts[0] if hosts else None
    interfaces = list((host or {}).get("interfaces") or [])
    snmp_interface = next(
        (
            interface
            for interface in interfaces
            if str(interface.get("type")) == "2" and str(interface.get("main")) == "1"
        ),
        None,
    ) or next(
        (interface for interface in interfaces if str(interface.get("type")) == "2"),
        None,
    )
    if not snmp_interface or not snmp_interface.get("interfaceid"):
        return False

    details = dict(snmp_interface.get("details") or {})
    community = str(details.get("community") or "{$SNMP_COMMUNITY}")
    raw_community = getattr(olt, "snmp_ro_community", None)
    if raw_community:
        try:
            community = decrypt_credential(raw_community) or str(raw_community)
        except Exception:
            logger.warning(
                "zabbix_olt_snmp_community_unreadable",
                extra={"olt_id": str(getattr(olt, "id", ""))},
            )

    details.update(
        {
            "version": _zabbix_snmp_version(getattr(olt, "snmp_version", None)),
            "bulk": "1" if bool(getattr(olt, "snmp_bulk_enabled", True)) else "0",
            "community": community,
        }
    )
    max_repetitions = getattr(olt, "snmp_bulk_max_repetitions", None)
    if max_repetitions is not None:
        details["max_repetitions"] = str(max_repetitions)

    return bool(
        client.update_host_interface(
            str(snmp_interface["interfaceid"]),
            ip=str(getattr(olt, "mgmt_ip", "") or snmp_interface.get("ip") or ""),
            dns=str(snmp_interface.get("dns") or ""),
            port=str(
                getattr(olt, "snmp_port", None) or snmp_interface.get("port") or "161"
            ),
            useip=1,
            details=details,
        )
    )


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

    host_name = _zabbix_host_key("olt", olt.hostname or olt.name)
    display_name = f"OLT: {olt.name}"

    try:
        # Get or create host group
        group_id = client.get_or_create_host_group(ZABBIX_GROUP_OLT)

        # Get template ID (optional)
        template_id = _get_template_id(client, ZABBIX_TEMPLATE_OLT_SNMP)
        template_ids = [template_id] if template_id else None

        olt.zabbix_host_id = _create_or_update_host(
            client,
            dotmac_id=str(olt.id),
            stored_host_id=olt.zabbix_host_id,
            host_name=host_name,
            display_name=display_name,
            group_id=group_id,
            template_ids=template_ids,
            interface_ip=olt.mgmt_ip,
            tags=_build_olt_tags(olt),
            inventory=_build_olt_inventory(olt),
            log_prefix="olt",
        )

        _sync_olt_snmp_interface(client, olt)
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

    host_name = _zabbix_host_key("nas", nas.code or nas.name)
    display_name = f"NAS: {nas.name}"

    try:
        # Get or create host group
        group_id = client.get_or_create_host_group(ZABBIX_GROUP_NAS)

        # Get template ID (optional)
        template_id = _get_template_id(client, ZABBIX_TEMPLATE_NAS_SNMP)
        template_ids = [template_id] if template_id else None

        nas.zabbix_host_id = _create_or_update_host(
            client,
            dotmac_id=str(nas.id),
            stored_host_id=nas.zabbix_host_id,
            host_name=host_name,
            display_name=display_name,
            group_id=group_id,
            template_ids=template_ids,
            interface_ip=mgmt_ip,
            tags=_build_nas_tags(nas),
            inventory=_build_nas_inventory(nas),
            log_prefix="nas",
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
        olt_id = str(olt.id)
        olt_name = olt.name
        if not olt.mgmt_ip:
            result.skipped.append(olt_name)
            continue

        try:
            with db.begin_nested():
                had_id = bool(olt.zabbix_host_id)
                host_id = sync_olt_to_zabbix(db, olt, client=client)

            if host_id:
                if had_id:
                    result.updated.append(olt_name)
                else:
                    result.created.append(olt_name)
            else:
                result.skipped.append(olt_name)
        except Exception as exc:
            result.failed.append((olt_name, str(exc)))
            logger.exception("olt_sync_exception", extra={"olt_id": olt_id})

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
        nas_id = str(nas.id)
        nas_name = nas.name
        mgmt_ip = nas.management_ip or nas.ip_address
        if not mgmt_ip:
            result.skipped.append(nas_name)
            continue

        try:
            with db.begin_nested():
                had_id = bool(nas.zabbix_host_id)
                host_id = sync_nas_to_zabbix(db, nas, client=client)

            if host_id:
                if had_id:
                    result.updated.append(nas_name)
                else:
                    result.created.append(nas_name)
            else:
                result.skipped.append(nas_name)
        except Exception as exc:
            result.failed.append((nas_name, str(exc)))
            logger.exception("nas_sync_exception", extra={"nas_id": nas_id})

    return result


def _disable_stale_hosts(
    db: Session,
    client: ZabbixClient,
    *,
    device_label: str,
    stale_rows: list,
) -> int:
    """Disable Zabbix hosts for devices that are no longer active.

    Hosts are disabled (``status=1``) rather than deleted so reactivating the
    device re-enables monitoring (``sync_*_to_zabbix`` sends ``status=0``). The
    ``zabbix_host_id`` is retained on the device for the same reason. Explicit
    decommissioning that should drop the host entirely goes through
    ``remove_device_from_zabbix``.
    """
    disabled = 0
    for device in stale_rows:
        device_id = str(device.id)
        host_id = device.zabbix_host_id
        if not host_id:
            continue
        try:
            with db.begin_nested():
                client.update_host(host_id=host_id, status=1)
                device.zabbix_last_sync_at = datetime.now(UTC)
                db.flush()
            disabled += 1
            logger.info(
                "zabbix_host_disabled_stale",
                extra={
                    "event": "zabbix_host_disabled_stale",
                    "device_type": device_label,
                    "device_id": device_id,
                    "zabbix_host_id": host_id,
                },
            )
        except Exception as exc:
            logger.warning(
                "zabbix_host_disable_stale_failed",
                extra={
                    "device_type": device_label,
                    "device_id": device_id,
                    "error": str(exc),
                },
            )
    return disabled


def disable_stale_olt_hosts(db: Session, client: ZabbixClient | None = None) -> int:
    """Disable Zabbix hosts for OLTs that are no longer active."""
    if client is None:
        client = _get_client()
    stmt = select(OLTDevice).where(
        OLTDevice.zabbix_host_id.is_not(None),
        or_(
            OLTDevice.is_active.is_(False),
            OLTDevice.status != DeviceStatus.active,
        ),
    )
    rows = list(db.scalars(stmt).all())
    return _disable_stale_hosts(db, client, device_label="olt", stale_rows=rows)


def disable_stale_nas_hosts(db: Session, client: ZabbixClient | None = None) -> int:
    """Disable Zabbix hosts for NAS devices that are no longer active."""
    if client is None:
        client = _get_client()
    stmt = select(NasDevice).where(
        NasDevice.zabbix_host_id.is_not(None),
        or_(
            NasDevice.is_active.is_(False),
            NasDevice.status != NasDeviceStatus.active,
        ),
    )
    rows = list(db.scalars(stmt).all())
    return _disable_stale_hosts(db, client, device_label="nas", stale_rows=rows)


def sync_all_devices(db: Session) -> dict[str, dict]:
    """Sync all OLTs and NAS devices to Zabbix.

    Active devices are created/updated (and re-enabled); devices that are no
    longer active have their Zabbix host disabled. Returns a summary of sync
    results for both device types.
    """
    olt_result = sync_all_olts(db)
    nas_result = sync_all_nas_devices(db)

    client = _get_client()
    olt_disabled = disable_stale_olt_hosts(db, client=client)
    nas_disabled = disable_stale_nas_hosts(db, client=client)

    olt_summary = olt_result.to_dict()
    nas_summary = nas_result.to_dict()
    olt_summary["disabled"] = olt_disabled
    nas_summary["disabled"] = nas_disabled

    logger.info(
        "zabbix_full_sync_complete",
        extra={
            "olt_created": len(olt_result.created),
            "olt_updated": len(olt_result.updated),
            "olt_failed": len(olt_result.failed),
            "olt_disabled": olt_disabled,
            "nas_created": len(nas_result.created),
            "nas_updated": len(nas_result.updated),
            "nas_failed": len(nas_result.failed),
            "nas_disabled": nas_disabled,
        },
    )

    return {
        "olt": olt_summary,
        "nas": nas_summary,
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
