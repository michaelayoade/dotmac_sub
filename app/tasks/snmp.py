import logging

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models.network_monitoring import NetworkDevice
from app.services.snmp_discovery import (
    apply_interface_snapshot,
    collect_interface_snapshot,
)
from app.services.vpn_routing import VpnRoutingError, ensure_vpn_ready

logger = logging.getLogger(__name__)


def _snmp_enabled_devices(session):
    return (
        session.query(NetworkDevice)
        .filter(NetworkDevice.snmp_enabled.is_(True))
        .filter(NetworkDevice.is_active.is_(True))
        .all()
    )


@celery_app.task(name="app.tasks.snmp.discover_interfaces")
def discover_interfaces() -> dict[str, int]:
    session = SessionLocal()
    try:
        created_total = 0
        updated_total = 0
        for device in _snmp_enabled_devices(session):
            if not device.mgmt_ip and not device.hostname:
                continue
            try:
                ensure_vpn_ready(session, getattr(device, "wireguard_server_id", None))
            except VpnRoutingError as exc:
                logger.warning("Skipping SNMP discovery for %s: %s", device.id, exc)
                continue
            snapshots = collect_interface_snapshot(device)
            created, updated = apply_interface_snapshot(
                session, device, snapshots, create_missing=True
            )
            created_total += created
            updated_total += updated
        return {"created": created_total, "updated": updated_total}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.snmp.walk_interfaces")
def walk_interfaces() -> dict[str, int]:
    session = SessionLocal()
    try:
        updated_total = 0
        for device in _snmp_enabled_devices(session):
            if not device.mgmt_ip and not device.hostname:
                continue
            try:
                ensure_vpn_ready(session, getattr(device, "wireguard_server_id", None))
            except VpnRoutingError as exc:
                logger.warning("Skipping SNMP walk for %s: %s", device.id, exc)
                continue
            snapshots = collect_interface_snapshot(device)
            _, updated = apply_interface_snapshot(
                session, device, snapshots, create_missing=False
            )
            updated_total += updated
        return {"updated": updated_total}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
