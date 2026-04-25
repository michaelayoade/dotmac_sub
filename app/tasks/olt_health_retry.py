"""Celery task to auto-retry failed OLT ping/SNMP connections."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)

# Cooldown to prevent retry storms (seconds)
RETRY_COOLDOWN_SECONDS = 30


@celery_app.task(name="app.tasks.olt_health_retry.retry_failed_olt_connections")
def retry_failed_olt_connections() -> dict[str, int]:
    """Retry ping/SNMP for OLTs that are in failed state.

    Finds OLTs with linked monitoring devices where last_ping_ok=False
    or last_snmp_ok=False, and attempts to reconnect.

    Returns:
        Statistics dict with retry counts.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload

    from app.models.network import OLTDevice
    from app.models.network_monitoring import NetworkDevice

    stats = {
        "checked": 0,
        "snmp_retried": 0,
        "snmp_recovered": 0,
        "ping_retried": 0,
        "ping_recovered": 0,
        "errors": 0,
    }

    try:
        with db_session_adapter.session() as db:
            # Get all active OLTs
            olts = list(
                db.scalars(
                    select(OLTDevice).where(OLTDevice.is_active.is_(True))
                ).all()
            )

            # Build lookup indexes for linked monitoring devices
            all_devices = list(
                db.scalars(
                    select(NetworkDevice).where(NetworkDevice.is_active.is_(True))
                ).all()
            )
            by_mgmt_ip = {d.mgmt_ip: d for d in all_devices if d.mgmt_ip}
            by_hostname = {d.hostname: d for d in all_devices if d.hostname}
            by_name = {d.name: d for d in all_devices if d.name}

            for olt in olts:
                # Find linked monitoring device
                linked = None
                if olt.mgmt_ip:
                    linked = by_mgmt_ip.get(olt.mgmt_ip)
                if linked is None and olt.hostname:
                    linked = by_hostname.get(olt.hostname)
                if linked is None and olt.name:
                    linked = by_name.get(olt.name)

                if not linked:
                    continue

                stats["checked"] += 1

                # Check if SNMP needs retry
                if linked.snmp_enabled and linked.last_snmp_ok is False:
                    stats["snmp_retried"] += 1
                    try:
                        recovered = _retry_snmp_check(db, linked)
                        if recovered:
                            stats["snmp_recovered"] += 1
                            logger.info(
                                "OLT %s SNMP recovered after retry", olt.name
                            )
                    except Exception as exc:
                        stats["errors"] += 1
                        logger.warning(
                            "SNMP retry failed for OLT %s: %s", olt.name, exc
                        )

                # Check if ping needs retry
                if linked.ping_enabled and linked.last_ping_ok is False:
                    stats["ping_retried"] += 1
                    try:
                        recovered = _retry_ping_check(db, linked)
                        if recovered:
                            stats["ping_recovered"] += 1
                            logger.info(
                                "OLT %s ping recovered after retry", olt.name
                            )
                    except Exception as exc:
                        stats["errors"] += 1
                        logger.warning(
                            "Ping retry failed for OLT %s: %s", olt.name, exc
                        )

            db.commit()

        if stats["snmp_recovered"] or stats["ping_recovered"]:
            logger.info(
                "OLT health retry complete: %d SNMP recovered, %d ping recovered",
                stats["snmp_recovered"],
                stats["ping_recovered"],
            )

        return stats

    except Exception as e:
        logger.error("OLT health retry task failed: %s", e, exc_info=True)
        raise


def _retry_snmp_check(db, device) -> bool:
    """Retry SNMP check for a device. Returns True if recovered."""
    from app.services import web_network_core_runtime as core_runtime_service

    try:
        updated_device, error = core_runtime_service.snmp_check_device(
            db, str(device.id)
        )
        if updated_device and updated_device.last_snmp_ok:
            return True
        return False
    except Exception:
        return False


def _retry_ping_check(db, device) -> bool:
    """Retry ping check for a device. Returns True if recovered."""
    from datetime import UTC, datetime

    import subprocess

    if not device.mgmt_ip:
        return False

    try:
        # Simple ICMP ping with 2 second timeout, 2 attempts
        result = subprocess.run(
            ["ping", "-c", "2", "-W", "2", device.mgmt_ip],
            capture_output=True,
            timeout=10,
        )
        now = datetime.now(UTC)
        device.last_ping_at = now

        if result.returncode == 0:
            device.last_ping_ok = True
            return True
        else:
            device.last_ping_ok = False
            return False
    except subprocess.TimeoutExpired:
        device.last_ping_ok = False
        return False
    except Exception:
        return False


@celery_app.task(
    name="app.tasks.olt_health_retry.retry_single_olt",
    bind=True,
    max_retries=2,
    default_retry_delay=RETRY_COOLDOWN_SECONDS,
)
def retry_single_olt(self, olt_id: str) -> dict[str, object]:
    """Retry ping/SNMP for a single OLT immediately after failure detection.

    This is triggered when an OLT transitions from healthy to failed state.
    Uses exponential backoff via Celery's retry mechanism.

    Args:
        olt_id: The OLT device UUID.

    Returns:
        Result dict with recovery status.
    """
    from sqlalchemy import select

    from app.models.network import OLTDevice
    from app.models.network_monitoring import NetworkDevice

    result: dict[str, object] = {
        "olt_id": olt_id,
        "snmp_recovered": False,
        "ping_recovered": False,
        "error": None,
    }

    try:
        with db_session_adapter.session() as db:
            olt = db.scalar(
                select(OLTDevice).where(
                    OLTDevice.id == olt_id,
                    OLTDevice.is_active.is_(True),
                )
            )
            if not olt:
                result["error"] = "OLT not found or inactive"
                return result

            # Find linked monitoring device
            all_devices = list(
                db.scalars(
                    select(NetworkDevice).where(NetworkDevice.is_active.is_(True))
                ).all()
            )
            by_mgmt_ip = {d.mgmt_ip: d for d in all_devices if d.mgmt_ip}
            by_hostname = {d.hostname: d for d in all_devices if d.hostname}
            by_name = {d.name: d for d in all_devices if d.name}

            linked = None
            if olt.mgmt_ip:
                linked = by_mgmt_ip.get(olt.mgmt_ip)
            if linked is None and olt.hostname:
                linked = by_hostname.get(olt.hostname)
            if linked is None and olt.name:
                linked = by_name.get(olt.name)

            if not linked:
                result["error"] = "No linked monitoring device found"
                return result

            # Check and retry SNMP if failed
            if linked.snmp_enabled and linked.last_snmp_ok is False:
                try:
                    if _retry_snmp_check(db, linked):
                        result["snmp_recovered"] = True
                        logger.info("OLT %s SNMP recovered via immediate retry", olt.name)
                except Exception as exc:
                    logger.warning(
                        "Immediate SNMP retry failed for OLT %s: %s", olt.name, exc
                    )

            # Check and retry ping if failed
            if linked.ping_enabled and linked.last_ping_ok is False:
                try:
                    if _retry_ping_check(db, linked):
                        result["ping_recovered"] = True
                        logger.info("OLT %s ping recovered via immediate retry", olt.name)
                except Exception as exc:
                    logger.warning(
                        "Immediate ping retry failed for OLT %s: %s", olt.name, exc
                    )

            db.commit()

            # If not recovered and we have retries left, schedule another attempt
            if (
                not result["snmp_recovered"]
                and not result["ping_recovered"]
                and (linked.last_snmp_ok is False or linked.last_ping_ok is False)
            ):
                try:
                    raise self.retry()
                except self.MaxRetriesExceededError:
                    logger.info(
                        "OLT %s immediate retries exhausted, waiting for scheduled retry",
                        olt.name,
                    )

        return result

    except Exception as e:
        result["error"] = str(e)
        logger.error("Single OLT retry task failed for %s: %s", olt_id, e, exc_info=True)
        return result


def trigger_immediate_retry(olt_id: str, delay_seconds: int = 5) -> None:
    """Trigger an immediate retry for a failed OLT.

    Call this when an OLT transitions from healthy to failed state.
    Uses a small delay to avoid hammering the device immediately.

    Args:
        olt_id: The OLT device UUID.
        delay_seconds: Seconds to wait before retry (default 5).
    """
    retry_single_olt.apply_async(
        args=[str(olt_id)],
        countdown=delay_seconds,
    )
