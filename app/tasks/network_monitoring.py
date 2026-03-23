"""Celery tasks for periodic network monitoring health refresh."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models.network_monitoring import NetworkDevice
from app.services import web_network_core_runtime as core_runtime_service
from app.services.network_vendor_polling import refresh_device_from_vendor_api

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.network_monitoring.refresh_core_device_ping")
def refresh_core_device_ping() -> dict[str, int]:
    """Refresh ping status for active devices with ping enabled."""
    session = SessionLocal()
    try:
        devices = (
            session.query(NetworkDevice)
            .filter(NetworkDevice.is_active.is_(True))
            .filter(NetworkDevice.ping_enabled.is_(True))
            .all()
        )
        summary = core_runtime_service.refresh_devices_health(
            session,
            devices,
            include_snmp=False,
            max_workers=12,
        )
        session.commit()
        return summary
    except Exception:
        session.rollback()
        logger.exception("Periodic ping refresh failed")
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.network_monitoring.refresh_core_device_snmp")
def refresh_core_device_snmp() -> dict[str, int]:
    """Refresh SNMP health + metrics for active SNMP-enabled devices."""
    session = SessionLocal()
    checked = 0
    updated = 0
    failed = 0
    try:
        devices = (
            session.query(NetworkDevice)
            .filter(NetworkDevice.is_active.is_(True))
            .filter(NetworkDevice.snmp_enabled.is_(True))
            .all()
        )
        for device in devices:
            checked += 1
            try:
                handled, success = refresh_device_from_vendor_api(session, device)
                if handled:
                    if success:
                        updated += 1
                    else:
                        failed += 1
                else:
                    core_runtime_service.snmp_check_device(session, str(device.id))
                    if device.last_snmp_ok:
                        core_runtime_service.discover_interfaces_and_health(
                            session, device
                        )
                        # Poll custom SNMP OIDs
                        try:
                            from app.services.monitoring_metrics import (
                                poll_custom_snmp_oids,
                            )

                            poll_custom_snmp_oids(session, device)
                        except Exception as exc:
                            logger.warning(
                                "Custom OID poll failed for %s: %s", device.id, exc
                            )
                        # Poll interface traffic counters for bandwidth
                        try:
                            from app.services.monitoring_metrics import (
                                poll_interface_traffic,
                            )

                            poll_interface_traffic(session, device)
                        except Exception as exc:
                            logger.warning(
                                "Interface traffic poll failed for %s: %s",
                                device.id,
                                exc,
                            )
                        # Update subscriber impact count
                        try:
                            from app.services.monitoring_metrics import (
                                update_device_subscriber_count,
                            )

                            update_device_subscriber_count(session, device)
                        except Exception as exc:
                            logger.warning(
                                "Subscriber count update failed for %s: %s",
                                device.id,
                                exc,
                            )
                        # Poll CPU/memory/temperature
                        try:
                            from app.services.monitoring_metrics import (
                                poll_device_system_metrics,
                            )

                            poll_device_system_metrics(session, device)
                        except Exception as exc:
                            logger.warning(
                                "System metrics poll failed for %s: %s", device.id, exc
                            )
                        updated += 1
                    else:
                        failed += 1
                session.commit()
            except Exception:
                session.rollback()
                failed += 1
                logger.exception("SNMP refresh failed for device %s", device.id)
                try:
                    device_fresh = session.get(NetworkDevice, device.id)
                    if device_fresh:
                        core_runtime_service.mark_discovery_failure(
                            session, device_fresh
                        )
                        session.commit()
                except Exception:
                    logger.warning(
                        "Failed to mark discovery failure for device %s", device.id
                    )
                    session.rollback()

        return {"checked": checked, "updated": updated, "failed": failed}
    except Exception:
        session.rollback()
        logger.exception("Periodic SNMP refresh failed")
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.network_monitoring.poll_onu_signals")
def poll_onu_signals() -> dict[str, int]:
    """Poll ONT/ONU optical signal strength from all active OLT inventory."""
    from app.services.network import olt_polling as olt_polling_service

    session = SessionLocal()
    try:
        result = olt_polling_service.poll_all_olts(session)
        olt_polling_service.push_signal_metrics_to_victoriametrics(session)
        session.commit()
        return {
            "olts_checked": int(result.get("olts_polled", 0)),
            "onus_polled": int(result.get("total_polled", 0)),
            "stored": int(result.get("total_updated", 0)),
            "errors": int(result.get("total_errors", 0)),
        }
    except Exception:
        session.rollback()
        logger.exception("ONU signal polling failed")
        raise
    finally:
        session.close()
