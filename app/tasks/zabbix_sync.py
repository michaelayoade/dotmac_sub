"""Celery tasks for Zabbix device synchronization.

This module provides background tasks for syncing DotMac network devices
(OLTs and NAS devices) to Zabbix monitoring hosts.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.zabbix import ZabbixClientError

logger = logging.getLogger(__name__)


def _zabbix_sync_enabled() -> bool:
    """Check if Zabbix sync is enabled and configured."""
    # Zabbix sync requires API token and URL
    return bool(os.getenv("ZABBIX_API_TOKEN")) and bool(os.getenv("ZABBIX_API_URL"))


@celery_app.task(
    name="app.tasks.zabbix_sync.sync_devices_to_zabbix",
    soft_time_limit=240,
    time_limit=300,
)
def sync_devices_to_zabbix() -> dict[str, Any]:
    """Periodic task to sync all DotMac devices to Zabbix hosts.

    This task runs every 5 minutes (configured in scheduler_config.py)
    and ensures that:
    - All active OLT devices have corresponding Zabbix hosts
    - All active NAS devices have corresponding Zabbix hosts
    - Device metadata (name, IP, vendor, model) is kept in sync
    - Deactivated devices have their Zabbix hosts disabled

    Returns:
        Dict with sync statistics for OLTs and NAS devices.
    """
    if not _zabbix_sync_enabled():
        logger.info(
            "zabbix_sync_skipped",
            extra={"event": "zabbix_sync_skipped", "reason": "not_configured"},
        )
        return {"skipped": "zabbix_not_configured"}

    db = db_session_adapter.create_session()
    try:
        from app.services.zabbix_host_sync import sync_all_devices

        result = sync_all_devices(db)
        db.commit()

        logger.info(
            "zabbix_sync_complete",
            extra={
                "event": "zabbix_sync_complete",
                "olt_created": result["olt"]["created"],
                "olt_updated": result["olt"]["updated"],
                "olt_failed": result["olt"]["failed"],
                "nas_created": result["nas"]["created"],
                "nas_updated": result["nas"]["updated"],
                "nas_failed": result["nas"]["failed"],
            },
        )

        return result

    except ZabbixClientError as exc:
        logger.warning(
            "zabbix_sync_failed",
            extra={"event": "zabbix_sync_failed", "error": str(exc)},
        )
        db.rollback()
        return {"error": "zabbix_unavailable", "message": str(exc)}

    except SoftTimeLimitExceeded:
        logger.warning(
            "zabbix_sync_timeout",
            extra={"event": "zabbix_sync_timeout"},
        )
        db.rollback()
        return {"error": "timeout"}

    except Exception as exc:
        logger.exception(
            "zabbix_sync_exception",
            extra={"event": "zabbix_sync_exception"},
        )
        db.rollback()
        return {"error": "exception", "message": str(exc)}

    finally:
        db.close()


@celery_app.task(
    name="app.tasks.zabbix_sync.sync_single_olt_to_zabbix",
    soft_time_limit=30,
    time_limit=60,
)
def sync_single_olt_to_zabbix(olt_id: str) -> dict[str, Any]:
    """Sync a single OLT to Zabbix immediately.

    Called when an OLT is created or updated to ensure Zabbix
    host is updated without waiting for the periodic sync.

    Args:
        olt_id: UUID of the OLT device as string.

    Returns:
        Dict with sync result.
    """
    if not _zabbix_sync_enabled():
        return {"skipped": "zabbix_not_configured"}

    from uuid import UUID

    db = db_session_adapter.create_session()
    try:
        from app.models.network import OLTDevice
        from app.services.zabbix_host_sync import sync_olt_to_zabbix

        olt = db.get(OLTDevice, UUID(olt_id))
        if not olt:
            return {"error": "olt_not_found", "olt_id": olt_id}

        zabbix_host_id = sync_olt_to_zabbix(db, olt)
        db.commit()

        if zabbix_host_id:
            logger.info(
                "zabbix_olt_sync_single_success",
                extra={
                    "event": "zabbix_olt_sync_single_success",
                    "olt_id": olt_id,
                    "zabbix_host_id": zabbix_host_id,
                },
            )
            return {"success": True, "olt_id": olt_id, "zabbix_host_id": zabbix_host_id}
        else:
            return {"success": False, "olt_id": olt_id, "reason": "sync_returned_none"}

    except ZabbixClientError as exc:
        logger.warning(
            "zabbix_olt_sync_single_failed",
            extra={"event": "zabbix_olt_sync_single_failed", "olt_id": olt_id, "error": str(exc)},
        )
        db.rollback()
        return {"error": "zabbix_unavailable", "olt_id": olt_id, "message": str(exc)}

    except Exception as exc:
        logger.exception(
            "zabbix_olt_sync_single_exception",
            extra={"event": "zabbix_olt_sync_single_exception", "olt_id": olt_id},
        )
        db.rollback()
        return {"error": "exception", "olt_id": olt_id, "message": str(exc)}

    finally:
        db.close()


@celery_app.task(
    name="app.tasks.zabbix_sync.sync_single_nas_to_zabbix",
    soft_time_limit=30,
    time_limit=60,
)
def sync_single_nas_to_zabbix(nas_id: str) -> dict[str, Any]:
    """Sync a single NAS device to Zabbix immediately.

    Called when a NAS device is created or updated to ensure Zabbix
    host is updated without waiting for the periodic sync.

    Args:
        nas_id: UUID of the NAS device as string.

    Returns:
        Dict with sync result.
    """
    if not _zabbix_sync_enabled():
        return {"skipped": "zabbix_not_configured"}

    from uuid import UUID

    db = db_session_adapter.create_session()
    try:
        from app.models.catalog import NasDevice
        from app.services.zabbix_host_sync import sync_nas_to_zabbix

        nas = db.get(NasDevice, UUID(nas_id))
        if not nas:
            return {"error": "nas_not_found", "nas_id": nas_id}

        zabbix_host_id = sync_nas_to_zabbix(db, nas)
        db.commit()

        if zabbix_host_id:
            logger.info(
                "zabbix_nas_sync_single_success",
                extra={
                    "event": "zabbix_nas_sync_single_success",
                    "nas_id": nas_id,
                    "zabbix_host_id": zabbix_host_id,
                },
            )
            return {"success": True, "nas_id": nas_id, "zabbix_host_id": zabbix_host_id}
        else:
            return {"success": False, "nas_id": nas_id, "reason": "sync_returned_none"}

    except ZabbixClientError as exc:
        logger.warning(
            "zabbix_nas_sync_single_failed",
            extra={"event": "zabbix_nas_sync_single_failed", "nas_id": nas_id, "error": str(exc)},
        )
        db.rollback()
        return {"error": "zabbix_unavailable", "nas_id": nas_id, "message": str(exc)}

    except Exception as exc:
        logger.exception(
            "zabbix_nas_sync_single_exception",
            extra={"event": "zabbix_nas_sync_single_exception", "nas_id": nas_id},
        )
        db.rollback()
        return {"error": "exception", "nas_id": nas_id, "message": str(exc)}

    finally:
        db.close()


@celery_app.task(
    name="app.tasks.zabbix_sync.remove_device_from_zabbix",
    soft_time_limit=30,
    time_limit=60,
)
def remove_device_from_zabbix_task(device_type: str, device_id: str) -> dict[str, Any]:
    """Remove a device's Zabbix host when decommissioned.

    Args:
        device_type: "olt" or "nas"
        device_id: UUID of the device as string.

    Returns:
        Dict with removal result.
    """
    if not _zabbix_sync_enabled():
        return {"skipped": "zabbix_not_configured"}

    from uuid import UUID

    db = db_session_adapter.create_session()
    try:
        from app.services.zabbix_host_sync import remove_device_from_zabbix

        success = remove_device_from_zabbix(db, device_type, UUID(device_id))
        db.commit()

        if success:
            logger.info(
                "zabbix_device_removed",
                extra={
                    "event": "zabbix_device_removed",
                    "device_type": device_type,
                    "device_id": device_id,
                },
            )
            return {"success": True, "device_type": device_type, "device_id": device_id}
        else:
            return {"success": False, "device_type": device_type, "device_id": device_id}

    except ZabbixClientError as exc:
        logger.warning(
            "zabbix_device_remove_failed",
            extra={
                "event": "zabbix_device_remove_failed",
                "device_type": device_type,
                "device_id": device_id,
                "error": str(exc),
            },
        )
        db.rollback()
        return {"error": "zabbix_unavailable", "message": str(exc)}

    except Exception as exc:
        logger.exception(
            "zabbix_device_remove_exception",
            extra={
                "event": "zabbix_device_remove_exception",
                "device_type": device_type,
                "device_id": device_id,
            },
        )
        db.rollback()
        return {"error": "exception", "message": str(exc)}

    finally:
        db.close()
