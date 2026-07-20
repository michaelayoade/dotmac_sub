"""ONT config snapshot capture and retrieval service.

Captures point-in-time TR-069 running configuration from an ONT and
stores it as a snapshot for historical tracking, change detection,
and audit purposes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.models.network import OntConfigSnapshot, OntUnit
from app.models.ont_observation import OntObservation
from app.services.common import coerce_uuid
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.ont_action_device import get_running_config

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_SECRET_KEY_MARKERS = (
    "password",
    "passphrase",
    "pre_shared_key",
    "presharedkey",
    "keypassphrase",
    "secret",
)
_SNAPSHOT_SCHEMA_VERSION = 2


def redact_snapshot_secrets(value):
    """Recursively remove credentials before snapshot persistence or display."""
    if isinstance(value, dict):
        return {
            key: (
                "[redacted]"
                if any(marker in str(key).lower() for marker in _SECRET_KEY_MARKERS)
                else redact_snapshot_secrets(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_snapshot_secrets(item) for item in value]
    return value


def _json_value(value):
    """Return a deterministic JSON-safe representation of snapshot evidence."""
    if isinstance(value, dict):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    if isinstance(value, (uuid.UUID, Enum)):
        return str(value.value if isinstance(value, Enum) else value)
    return value


def _snapshot_checksum(payload: dict) -> str:
    encoded = json.dumps(
        _json_value(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _observation_payload(observation: OntObservation | None) -> dict:
    if observation is None:
        return {"available": False, "olt": None, "acs": None}
    return {
        "available": True,
        "last_reconciled_at": observation.last_reconciled_at,
        "last_reconcile_duration_ms": observation.last_reconcile_duration_ms,
        "management_ip_pingable": observation.mgmt_ip_pingable,
        "olt": {
            "present": observation.olt_present,
            "match_state": observation.olt_match_state,
            "run_state": observation.olt_run_state,
            "distance_m": observation.olt_distance_m,
            "rx_dbm": observation.olt_rx_dbm,
            "tx_dbm": observation.olt_tx_dbm,
            "temperature_c": observation.olt_temperature_c,
            "description": observation.olt_description,
            "management_ip": observation.olt_mgmt_ip,
            "management_vlan": observation.olt_mgmt_vlan,
            "line_profile_id": observation.olt_line_profile_id,
            "service_profile_id": observation.olt_service_profile_id,
            "tr069_profile_id": observation.olt_tr069_profile_id,
            "service_ports": observation.olt_service_ports or [],
        },
        "acs": {
            "present": observation.acs_present,
            "last_inform_at": observation.acs_last_inform_at,
            "software_version": observation.acs_observed_software_version,
            "pppoe_username": observation.acs_observed_pppoe_username,
            "pppoe_enabled": observation.acs_observed_pppoe_enable,
            "wan_vlan": observation.acs_observed_wan_vlan,
            "wan_external_ip": observation.acs_observed_wan_external_ip,
            "wan_connection_status": observation.acs_observed_wan_connection_status,
            "nat_enabled": observation.acs_observed_nat_enabled,
            "dhcp_enabled": observation.acs_observed_dhcp_enabled,
            "ssid": observation.acs_observed_ssid,
            "wifi_enabled": observation.acs_observed_wifi_enabled,
            "wifi_channel": observation.acs_observed_wifi_channel,
            "wifi_security_mode": observation.acs_observed_wifi_security_mode,
            "remote_ssh_enabled": observation.acs_observed_remote_ssh_enabled,
            "remote_ssh_port": observation.acs_observed_remote_ssh_port,
            "remote_telnet_enabled": observation.acs_observed_remote_telnet_enabled,
            "remote_telnet_port": observation.acs_observed_remote_telnet_port,
            "data_model_root": observation.acs_data_model_root,
            "ipv6_enabled": observation.acs_observed_ipv6_enabled,
            "wan_ip_enabled": observation.acs_observed_wan_ip_enable,
            "wan_addressing_type": observation.acs_observed_wan_addressing_type,
            "wan_ip_address": observation.acs_observed_wan_ip_address,
            "wan_subnet_mask": observation.acs_observed_wan_subnet_mask,
            "wan_gateway": observation.acs_observed_wan_gateway,
            "wan_dns_servers": observation.acs_observed_wan_dns_servers,
            "dhcpv6_enabled": observation.acs_observed_dhcpv6_enabled,
            "dhcpv6_request_prefixes": (
                observation.acs_observed_dhcpv6_request_prefixes
            ),
            "ra_enabled": observation.acs_observed_ra_enabled,
        },
    }


def _effective_config_payload(effective: object) -> dict:
    if not isinstance(effective, dict):
        return {}
    config_pack = effective.get("config_pack")
    assignment = effective.get("assignment")
    return {
        "config_pack_id": getattr(config_pack, "id", None),
        "assignment_id": getattr(assignment, "id", None),
        "desired_config_keys": effective.get("desired_config_keys") or [],
        "values": effective.get("values") or {},
    }


def snapshot_integrity_valid(snapshot: OntConfigSnapshot) -> bool | None:
    """Verify a versioned snapshot, or return None for legacy snapshots."""
    if not snapshot.payload_checksum:
        return None
    payload = {
        "ont_unit_id": str(snapshot.ont_unit_id),
        "source": snapshot.source,
        "label": snapshot.label,
        "schema_version": snapshot.schema_version,
        "device_info": snapshot.device_info,
        "wan": snapshot.wan,
        "optical": snapshot.optical,
        "wifi": snapshot.wifi,
        "effective_config": snapshot.effective_config,
        "observed_state": snapshot.observed_state,
        "provenance": snapshot.provenance,
    }
    return _snapshot_checksum(payload) == snapshot.payload_checksum


def _safe_uuid(value: str, label: str = "ID") -> uuid.UUID:
    """Validate and coerce a string to UUID, raising 400 on failure."""
    try:
        result = coerce_uuid(value)
        if result is None:
            raise ValueError("None result")
        return result
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"Invalid {label}: {value!r}")


class OntConfigSnapshots:
    """Manager for ONT config snapshots."""

    snapshot_integrity_valid = staticmethod(snapshot_integrity_valid)

    @staticmethod
    def capture(
        db: Session,
        ont_id: str,
        *,
        source: str = "composite",
        label: str | None = None,
    ) -> OntConfigSnapshot:
        """Fetch running config from TR-069 and save as snapshot.

        Args:
            db: Database session.
            ont_id: ONT unit ID.
            source: Snapshot capture method identifier.
            label: Optional operator note for the snapshot.

        Returns:
            Created OntConfigSnapshot.

        Raises:
            HTTPException: If config retrieval or storage fails.
        """
        ont_uuid = _safe_uuid(ont_id, "ONT ID")

        ont = db.get(OntUnit, ont_uuid)
        if ont is None:
            raise HTTPException(status_code=404, detail="ONT not found")

        result = get_running_config(db, ont_id)
        if not result.success or not result.data:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to capture config: {result.message}",
            )

        captured_at = datetime.now(UTC)
        observation = db.scalars(
            select(OntObservation).where(OntObservation.ont_unit_id == ont_uuid)
        ).first()
        effective = resolve_effective_ont_config(db, ont)
        acs_sections = {
            "device_info": redact_snapshot_secrets(result.data.get("device_info")),
            "wan": redact_snapshot_secrets(result.data.get("wan")),
            "optical": redact_snapshot_secrets(result.data.get("optical")),
            "wifi": redact_snapshot_secrets(result.data.get("wifi")),
        }
        effective_config = redact_snapshot_secrets(
            _json_value(_effective_config_payload(effective))
        )
        observed_state = redact_snapshot_secrets(
            _json_value(_observation_payload(observation))
        )
        provenance = _json_value(
            {
                "captured_at": captured_at,
                "target": {
                    "ont_unit_id": ont.id,
                    "serial_number": ont.serial_number,
                    "olt_device_id": ont.olt_device_id,
                },
                "acs_running_config": {
                    "status": "live",
                    "captured_at": captured_at,
                },
                "effective_config": {
                    "status": "resolved",
                    "captured_at": captured_at,
                },
                "reconciler_observation": {
                    "status": "cached" if observation else "missing",
                    "observed_at": (
                        observation.last_reconciled_at if observation else None
                    ),
                },
            }
        )
        payload = {
            "schema_version": _SNAPSHOT_SCHEMA_VERSION,
            **acs_sections,
            "effective_config": effective_config,
            "observed_state": observed_state,
            "provenance": provenance,
        }
        checksum_payload = {
            "ont_unit_id": str(ont_uuid),
            "source": source,
            "label": label,
            **payload,
        }
        snapshot = OntConfigSnapshot(
            ont_unit_id=ont_uuid,
            source=source,
            label=label,
            **payload,
            payload_checksum=_snapshot_checksum(checksum_payload),
        )
        db.add(snapshot)
        try:
            db.commit()
            db.refresh(snapshot)
        except SQLAlchemyError as exc:
            db.rollback()
            logger.error("Failed to save config snapshot for ONT %s: %s", ont_id, exc)
            raise HTTPException(
                status_code=500,
                detail="Config was retrieved but could not be saved to database.",
            )
        logger.info("Config snapshot captured for ONT %s (source=%s)", ont_id, source)
        return snapshot

    @staticmethod
    def list_for_ont(
        db: Session, ont_id: str, *, limit: int = 20
    ) -> list[OntConfigSnapshot]:
        """List snapshots for an ONT, newest first."""
        ont_uuid = _safe_uuid(ont_id, "ONT ID")
        stmt = (
            select(OntConfigSnapshot)
            .where(OntConfigSnapshot.ont_unit_id == ont_uuid)
            .order_by(OntConfigSnapshot.created_at.desc())
            .limit(limit)
        )
        return list(db.scalars(stmt).all())

    @staticmethod
    def get(
        db: Session, snapshot_id: str, *, ont_id: str | None = None
    ) -> OntConfigSnapshot:
        """Get a single snapshot by ID, optionally verifying ONT ownership."""
        snap_uuid = _safe_uuid(snapshot_id, "Snapshot ID")
        snapshot = db.get(OntConfigSnapshot, snap_uuid)
        if not snapshot:
            raise HTTPException(status_code=404, detail="Snapshot not found")
        if ont_id and str(snapshot.ont_unit_id) != str(_safe_uuid(ont_id, "ONT ID")):
            raise HTTPException(
                status_code=404, detail="Snapshot not found for this ONT"
            )
        return snapshot

    @staticmethod
    def delete(db: Session, snapshot_id: str, *, ont_id: str | None = None) -> bool:
        """Delete a snapshot, optionally verifying ONT ownership."""
        snap_uuid = _safe_uuid(snapshot_id, "Snapshot ID")
        snapshot = db.get(OntConfigSnapshot, snap_uuid)
        if not snapshot:
            raise HTTPException(status_code=404, detail="Snapshot not found")
        if ont_id and str(snapshot.ont_unit_id) != str(_safe_uuid(ont_id, "ONT ID")):
            raise HTTPException(
                status_code=404, detail="Snapshot not found for this ONT"
            )
        db.delete(snapshot)
        try:
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            logger.error("Failed to delete config snapshot %s: %s", snapshot_id, exc)
            raise HTTPException(status_code=500, detail="Failed to delete snapshot.")
        logger.info("Config snapshot %s deleted", snapshot_id)
        return True


ont_config_snapshots = OntConfigSnapshots()
