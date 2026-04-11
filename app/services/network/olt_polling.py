"""OLT optical signal polling service.

Polls OLT devices via SNMP to collect per-ONT optical signal levels,
online status, and distance estimates. Updates OntUnit records in bulk.
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.network import (
    OltCard,
    OltCardPort,
    OLTDevice,
    OltSfpModule,
    OltShelf,
    OntAssignment,
    OntUnit,
    OnuOfflineReason,
    OnuOnlineStatus,
    PollStatus,
    PonPort,
)
from app.models.network_monitoring import NetworkDevice
from app.services.credential_crypto import decrypt_credential
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.network.olt_polling_metrics import (
    _push_olt_health_metrics,
)
from app.services.network.olt_polling_metrics import (
    push_signal_metrics_to_victoriametrics as push_signal_metrics_to_victoriametrics,  # noqa: F401 — re-export
)
from app.services.network.olt_polling_oids import (
    _SIGNAL_SENTINELS as _SIGNAL_SENTINELS,  # noqa: F401 — re-export
)
from app.services.network.olt_polling_oids import (
    _VENDOR_OID_OIDS as _VENDOR_OID_OIDS,  # noqa: F401 — re-export
)
from app.services.network.olt_polling_oids import (
    GENERIC_OIDS as GENERIC_OIDS,  # noqa: F401 — re-export
)
from app.services.network.olt_polling_oids import (
    _get_ddm_scales,
    _get_signal_scale,
    _resolve_oid_set,
)
from app.services.network.olt_polling_parsers import (
    _DDM_BIAS_MAX_MA,
    _DDM_BIAS_MIN_MA,
    _DDM_TEMP_MAX_C,
    _DDM_TEMP_MIN_C,
    _DDM_VOLTAGE_MAX_V,
    _DDM_VOLTAGE_MIN_V,
    OltHealthReading,
    OntSignalReading,
    SNMPValidationError,
    _derive_offline_reason,
    _fsp_hint_from_index,
    _fsp_hint_from_ont,
    _parse_ddm_value,
    _parse_distance,
    _parse_numeric,
    _parse_online_status,
    _parse_signal_value,
    _parse_snmp_table,
    _parse_sysdescr,
    _parse_uptime_ticks,
    _reading_sort_key,
    _run_olt_snmpwalk,
    _snmpget_value,
)
from app.services.network.olt_polling_parsers import (
    _split_onu_index as _split_onu_index,  # noqa: F401 — re-export
)
from app.services.network.ont_status import (
    resolve_acs_online_window_minutes_for_model,
    resolve_ont_status_snapshot,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Signal threshold classification
# ---------------------------------------------------------------------------

# Default thresholds — overridden by settings at runtime
DEFAULT_WARN_THRESHOLD = -25.0  # dBm
DEFAULT_CRIT_THRESHOLD = -28.0  # dBm

SIGNAL_QUALITY_GOOD = "good"
SIGNAL_QUALITY_WARNING = "warning"
SIGNAL_QUALITY_CRITICAL = "critical"
SIGNAL_QUALITY_UNKNOWN = "unknown"


def classify_signal(
    dbm: float | None,
    *,
    warn_threshold: float = DEFAULT_WARN_THRESHOLD,
    crit_threshold: float = DEFAULT_CRIT_THRESHOLD,
) -> str:
    """Classify optical signal quality based on dBm value.

    Args:
        dbm: Optical power in dBm (negative values).
        warn_threshold: dBm value below which signal is 'warning'.
        crit_threshold: dBm value below which signal is 'critical'.

    Returns:
        One of: 'good', 'warning', 'critical', 'unknown'.
    """
    if dbm is None:
        return SIGNAL_QUALITY_UNKNOWN
    if dbm >= warn_threshold:
        return SIGNAL_QUALITY_GOOD
    if dbm >= crit_threshold:
        return SIGNAL_QUALITY_WARNING
    return SIGNAL_QUALITY_CRITICAL


def get_signal_thresholds(db: Session) -> tuple[float, float]:
    """Load signal thresholds from settings, falling back to defaults."""
    try:
        from app.models.domain_settings import SettingDomain
        from app.services.settings_spec import resolve_value

        warn_raw = resolve_value(
            db, SettingDomain.network_monitoring, "ont_signal_warning_dbm"
        )
        crit_raw = resolve_value(
            db, SettingDomain.network_monitoring, "ont_signal_critical_dbm"
        )
        warn = float(str(warn_raw)) if warn_raw is not None else DEFAULT_WARN_THRESHOLD
        crit = float(str(crit_raw)) if crit_raw is not None else DEFAULT_CRIT_THRESHOLD
        return warn, crit
    except Exception as exc:
        logger.warning("Failed to load signal thresholds, using defaults: %s", exc)
        return DEFAULT_WARN_THRESHOLD, DEFAULT_CRIT_THRESHOLD


# Delta threshold: alert if signal changes by more than this amount between polls
_DEFAULT_SIGNAL_DELTA_DB = 3.0

_DEFAULT_ALERT_COOLDOWN_MINUTES = 30
_DEFAULT_OFFLINE_POLL_THRESHOLD = 2

# DDM health thresholds — alert when exceeded
_DDM_TEMPERATURE_WARN_C = 65.0
_DDM_TEMPERATURE_CRIT_C = 75.0
_DDM_VOLTAGE_LOW_V = 3.0
_DDM_VOLTAGE_HIGH_V = 3.6
_DDM_BIAS_CURRENT_WARN_MA = 60.0


def _get_alert_cooldown_seconds(db: Session) -> int:
    """Load signal alert cooldown from settings (in seconds)."""
    try:
        from app.models.domain_settings import SettingDomain
        from app.services.settings_spec import resolve_value

        raw = resolve_value(
            db, SettingDomain.network_monitoring, "ont_signal_alert_cooldown_minutes"
        )
        minutes = int(str(raw)) if raw is not None else _DEFAULT_ALERT_COOLDOWN_MINUTES
        return max(minutes, 5) * 60
    except Exception:
        return _DEFAULT_ALERT_COOLDOWN_MINUTES * 60


def _get_offline_poll_threshold(db: Session) -> int:
    """Load offline poll threshold from settings (consecutive polls before offline event)."""
    try:
        from app.models.domain_settings import SettingDomain
        from app.services.settings_spec import resolve_value

        raw = resolve_value(
            db, SettingDomain.network_monitoring, "ont_offline_poll_threshold"
        )
        threshold = int(str(raw)) if raw is not None else _DEFAULT_OFFLINE_POLL_THRESHOLD
        return max(threshold, 1)
    except Exception:
        return _DEFAULT_OFFLINE_POLL_THRESHOLD


# ---------------------------------------------------------------------------
# Reading-to-ONT mapping
# ---------------------------------------------------------------------------


def _build_reading_targets(
    db: Session,
    *,
    olt: OLTDevice,
    readings: list[OntSignalReading],
    assignments: list[OntAssignment],
) -> list[tuple[OntUnit, OntSignalReading]]:
    """Map SNMP readings to ONTs using external_id/FSP hints, then fallback order."""
    assignment_ont_ids = [
        ont_id for ont_id in (a.ont_unit_id for a in assignments) if ont_id is not None
    ]
    direct_ont_ids = list(
        db.scalars(
            select(OntUnit.id)
            .where(OntUnit.olt_device_id == olt.id)
            .where(OntUnit.is_active.is_(True))
        ).all()
    )

    ordered_ids: list = []
    seen_ids: set = set()
    for ont_id in assignment_ont_ids + direct_ont_ids:
        if ont_id in seen_ids:
            continue
        seen_ids.add(ont_id)
        ordered_ids.append(ont_id)

    if not ordered_ids:
        return []

    id_to_ont: dict = {
        ont.id: ont
        for ont in db.scalars(select(OntUnit).where(OntUnit.id.in_(ordered_ids))).all()
    }
    ordered_onts = [id_to_ont[ont_id] for ont_id in ordered_ids if ont_id in id_to_ont]
    if not ordered_onts:
        return []

    by_external_id: dict[str, OntUnit] = {}
    by_fsp_hint: dict[str, list[OntUnit]] = {}
    # P1 FIX: Add serial number index for more reliable matching
    by_serial: dict[str, OntUnit] = {}

    def _normalize_serial(val: str | None) -> str:
        """Normalize serial number for matching (strip non-alphanumeric, uppercase)."""
        import re
        return re.sub(r"[^A-Za-z0-9]", "", str(val or "").strip()).upper()

    for ont in ordered_onts:
        # Skip inactive ONTs to prevent stale records from blocking active ones
        if not getattr(ont, "is_active", True):
            continue
        external_id = str(getattr(ont, "external_id", "") or "").strip().lower()
        if external_id:
            by_external_id[external_id] = ont
        # P1 FIX: Index by normalized serial number for reliable matching
        ont_serial = _normalize_serial(getattr(ont, "serial_number", ""))
        if ont_serial and not ont_serial.startswith(("HW-", "ZT-", "NK-", "OLT-")):
            # Only index real serials, not synthetic ones
            by_serial[ont_serial] = ont
        fsp_hint = _fsp_hint_from_ont(ont)
        if fsp_hint:
            by_fsp_hint.setdefault(fsp_hint, []).append(ont)

    used_ont_ids: set = set()
    targets: list[tuple[OntUnit, OntSignalReading]] = []
    vendor_lower = str(getattr(olt, "vendor", "") or "").lower()

    ambiguous_fsp_matches = 0
    serial_matches = 0
    for reading in sorted(readings, key=lambda r: _reading_sort_key(r.onu_index)):
        matched: OntUnit | None = None

        # 1) Exact external_id match (preferred, deterministic)
        external_candidates = []
        if "huawei" in vendor_lower:
            external_candidates.append(f"huawei:{reading.onu_index}")
        if "zte" in vendor_lower:
            external_candidates.append(f"zte:{reading.onu_index}")
        if "nokia" in vendor_lower:
            external_candidates.append(f"nokia:{reading.onu_index}")
        external_candidates.append(reading.onu_index)

        for candidate in external_candidates:
            ont = by_external_id.get(candidate.lower())
            if ont and ont.id not in used_ont_ids:
                matched = ont
                break

        # P1 FIX: 2) Serial number match from SNMP (more reliable than FSP)
        if matched is None and reading.serial_number_raw:
            snmp_serial = _normalize_serial(reading.serial_number_raw)
            if snmp_serial:
                ont = by_serial.get(snmp_serial)
                if ont and ont.id not in used_ont_ids:
                    matched = ont
                    serial_matches += 1

        # 3) FSP hint match (frame/slot/port) - least reliable, only for single matches
        if matched is None:
            fsp_hint = _fsp_hint_from_index(reading.onu_index)
            if fsp_hint:
                unmatched = [
                    ont for ont in by_fsp_hint.get(fsp_hint, []) if ont.id not in used_ont_ids
                ]
                if len(unmatched) == 1:
                    matched = unmatched[0]
                elif len(unmatched) > 1:
                    ambiguous_fsp_matches += 1

        if matched is None:
            continue

        used_ont_ids.add(matched.id)
        targets.append((matched, reading))

    if ambiguous_fsp_matches:
        logger.warning(
            "Skipped %d SNMP readings on OLT %s due to ambiguous FSP-only matches",
            ambiguous_fsp_matches,
            getattr(olt, "name", "unknown"),
        )

    if serial_matches:
        logger.debug(
            "Matched %d ONTs by serial number on OLT %s",
            serial_matches,
            getattr(olt, "name", "unknown"),
        )

    return targets


def _get_olt_snmp_config(db: Session, olt: OLTDevice) -> dict[str, str | int | None]:
    """Build SNMP config dict for an OLT device.

    Checks the OLT's own snmp_ro_community first, then falls back to a
    linked NetworkDevice record resolved by mgmt_ip/hostname/name.
    """
    host = olt.mgmt_ip or olt.hostname
    vendor = (olt.vendor or "").lower()
    community: str | None = None

    # 1. Prefer SNMP community stored directly on the OLT device
    raw_olt_community = getattr(olt, "snmp_ro_community", None)
    if raw_olt_community:
        raw_olt_community = raw_olt_community.strip()
    if raw_olt_community:
        community = decrypt_credential(raw_olt_community)

    # 2. Fallback: resolve a linked NetworkDevice for SNMP credentials
    if not community:
        linked: NetworkDevice | None = None
        if olt.mgmt_ip:
            linked = db.scalars(
                select(NetworkDevice)
                .where(NetworkDevice.mgmt_ip == olt.mgmt_ip)
                .limit(1)
            ).first()
        if linked is None and olt.hostname:
            linked = db.scalars(
                select(NetworkDevice)
                .where(NetworkDevice.hostname == olt.hostname)
                .limit(1)
            ).first()
        if linked is None and olt.name:
            linked = db.scalars(
                select(NetworkDevice).where(NetworkDevice.name == olt.name).limit(1)
            ).first()

        if linked and linked.snmp_enabled:
            if (linked.snmp_version or "v2c").lower() != "v2c":
                logger.warning(
                    "Skipping OLT %s SNMP poll: unsupported SNMP version '%s' (only v2c supported)",
                    olt.name,
                    linked.snmp_version,
                )
            else:
                raw_community = (linked.snmp_community or "").strip() or None
                community = decrypt_credential(raw_community) if raw_community else None

    return {
        "host": host,
        "vendor": vendor,
        "community": community,
    }


# ---------------------------------------------------------------------------
# Main polling orchestration
# ---------------------------------------------------------------------------


# OLT hardware health OIDs (per-vendor)
_OLT_HEALTH_OIDS: dict[str, dict[str, str]] = {
    "huawei": {
        "cpu": ".1.3.6.1.4.1.2011.6.3.4.1.2.0",  # hwAvgDuty1min
        "temperature": ".1.3.6.1.4.1.2011.6.3.4.1.3.0",  # hwEntityTemperature
        "memory": ".1.3.6.1.4.1.2011.6.3.4.1.8.0",  # hwMemoryUtilization
        "uptime": ".1.3.6.1.2.1.1.3.0",  # sysUpTime (standard)
    },
    "zte": {
        "cpu": ".1.3.6.1.4.1.3902.1082.500.1.2.1.0",
        "temperature": ".1.3.6.1.4.1.3902.1082.500.1.2.2.0",
        "memory": ".1.3.6.1.4.1.3902.1082.500.1.2.3.0",
        "uptime": ".1.3.6.1.2.1.1.3.0",
    },
    "nokia": {
        "cpu": ".1.3.6.1.4.1.637.61.1.9.37.0",
        "temperature": ".1.3.6.1.4.1.637.61.1.9.50.0",
        "memory": ".1.3.6.1.4.1.637.61.1.9.38.0",
        "uptime": ".1.3.6.1.2.1.1.3.0",
    },
}

# Standard MIB-II fallback
_GENERIC_HEALTH_OIDS: dict[str, str] = {
    "uptime": ".1.3.6.1.2.1.1.3.0",  # sysUpTime
}

# sysDescr OID for auto-detection
_SYSDESCR_OID = ".1.3.6.1.2.1.1.1.0"


def poll_olt_ont_signals(
    db: Session,
    olt: OLTDevice,
    *,
    community: str | None = None,
) -> dict[str, int | str]:
    """Poll all ONTs on an OLT for optical signal levels via SNMP.

    Updates OntUnit records with signal data in bulk.

    Args:
        db: Database session.
        olt: OLT device to poll.
        community: SNMP community string.

    Returns:
        Stats dict: {polled, updated, errors, skipped}.
    """
    host = olt.mgmt_ip or olt.hostname
    if not host:
        logger.warning("OLT %s has no management IP or hostname, skipping", olt.name)
        return {"polled": 0, "updated": 0, "errors": 0, "skipped": 1}
    if not community:
        logger.warning("OLT %s has no SNMP community configured, skipping", olt.name)
        return {"polled": 0, "updated": 0, "errors": 0, "skipped": 1}

    # P0: Validate SNMP parameters early to fail fast on invalid input
    from app.services.network.olt_polling_parsers import (
        _validate_snmp_community,
        _validate_snmp_host,
    )

    try:
        host = _validate_snmp_host(host)
        community = _validate_snmp_community(community)
    except SNMPValidationError as e:
        logger.warning("OLT %s SNMP validation failed: %s", olt.name, e)
        return {"polled": 0, "updated": 0, "errors": 1, "skipped": 0, "validation_error": str(e)}

    vendor = (olt.vendor or "").lower()
    oids = _resolve_oid_set(vendor)
    scale = _get_signal_scale(vendor)

    # Walk signal tables from OLT
    olt_rx_raw = _parse_snmp_table(
        _run_olt_snmpwalk(host, oids["olt_rx"], community),
        base_oid=oids["olt_rx"],
    )
    onu_rx_raw = _parse_snmp_table(
        _run_olt_snmpwalk(host, oids["onu_rx"], community),
        base_oid=oids["onu_rx"],
    )
    distance_raw = (
        _parse_snmp_table(
            _run_olt_snmpwalk(host, oids.get("distance", ""), community),
            base_oid=oids.get("distance", ""),
        )
        if oids.get("distance")
        else {}
    )
    status_raw = (
        _parse_snmp_table(
            _run_olt_snmpwalk(host, oids.get("status", ""), community),
            base_oid=oids.get("status", ""),
        )
        if oids.get("status")
        else {}
    )

    # DDM health telemetry walks (optional — missing OIDs are silently skipped)
    onu_tx_raw = (
        _parse_snmp_table(
            _run_olt_snmpwalk(host, oids["onu_tx"], community),
            base_oid=oids["onu_tx"],
        )
        if oids.get("onu_tx")
        else {}
    )
    temperature_raw = (
        _parse_snmp_table(
            _run_olt_snmpwalk(host, oids["temperature"], community),
            base_oid=oids["temperature"],
        )
        if oids.get("temperature")
        else {}
    )
    voltage_raw = (
        _parse_snmp_table(
            _run_olt_snmpwalk(host, oids["voltage"], community),
            base_oid=oids["voltage"],
        )
        if oids.get("voltage")
        else {}
    )
    bias_current_raw = (
        _parse_snmp_table(
            _run_olt_snmpwalk(host, oids["bias_current"], community),
            base_oid=oids["bias_current"],
        )
        if oids.get("bias_current")
        else {}
    )
    offline_reason_snmp_raw = (
        _parse_snmp_table(
            _run_olt_snmpwalk(host, oids["offline_reason"], community),
            base_oid=oids["offline_reason"],
        )
        if oids.get("offline_reason")
        else {}
    )
    serial_number_raw = (
        _parse_snmp_table(
            _run_olt_snmpwalk(host, oids["serial_number"], community),
            base_oid=oids["serial_number"],
        )
        if oids.get("serial_number")
        else {}
    )

    if not olt_rx_raw and not onu_rx_raw and not status_raw:
        logger.info("No SNMP signal data returned for OLT %s (%s)", olt.name, host)
        return {"polled": 0, "updated": 0, "errors": 0, "skipped": 0}

    # Build readings keyed by ONU index
    all_indexes = (
        set(olt_rx_raw.keys()) | set(onu_rx_raw.keys()) | set(status_raw.keys())
    )
    readings: list[OntSignalReading] = []
    parse_stats: dict[str, int] = {
        "parsed": 0,
        "sentinel": 0,
        "out_of_range": 0,
        "missing": 0,
        "parse_error": 0,
    }
    ddm_scales = _get_ddm_scales(vendor)
    for idx in all_indexes:
        readings.append(
            OntSignalReading(
                onu_index=idx,
                olt_rx_dbm=_parse_signal_value(
                    olt_rx_raw.get(idx, ""),
                    scale,
                    vendor=vendor,
                    metric="olt_rx",
                    stats=parse_stats,
                ),
                onu_rx_dbm=_parse_signal_value(
                    onu_rx_raw.get(idx, ""),
                    scale,
                    vendor=vendor,
                    metric="onu_rx",
                    stats=parse_stats,
                ),
                onu_tx_dbm=_parse_signal_value(
                    onu_tx_raw.get(idx, ""),
                    scale,
                    vendor=vendor,
                    metric="onu_tx",
                    stats=parse_stats,
                ),
                distance_m=_parse_distance(distance_raw.get(idx, "")),
                is_online=_parse_online_status(status_raw.get(idx, "")),
                temperature_c=_parse_ddm_value(
                    temperature_raw.get(idx, ""),
                    scale=ddm_scales.get("temperature", 1.0),
                    min_value=_DDM_TEMP_MIN_C,
                    max_value=_DDM_TEMP_MAX_C,
                    metric="temperature",
                ),
                voltage_v=_parse_ddm_value(
                    voltage_raw.get(idx, ""),
                    scale=ddm_scales.get("voltage", 0.01),
                    min_value=_DDM_VOLTAGE_MIN_V,
                    max_value=_DDM_VOLTAGE_MAX_V,
                    metric="voltage",
                ),
                bias_current_ma=_parse_ddm_value(
                    bias_current_raw.get(idx, ""),
                    scale=ddm_scales.get("bias_current", 0.001),
                    min_value=_DDM_BIAS_MIN_MA,
                    max_value=_DDM_BIAS_MAX_MA,
                    metric="bias_current",
                ),
                offline_reason_raw=offline_reason_snmp_raw.get(idx),
                serial_number_raw=serial_number_raw.get(idx),
            )
        )

    polled = len(readings)
    logger.info(
        "Polled %d ONT signal readings from OLT %s (parsed=%d sentinel=%d out_of_range=%d)",
        polled,
        olt.name,
        parse_stats.get("parsed", 0),
        parse_stats.get("sentinel", 0),
        parse_stats.get("out_of_range", 0),
    )

    # Map readings to OntUnit records via assignments
    # Get all active assignments for this OLT's PON ports
    stmt = (
        select(OntAssignment)
        .join(PonPort, OntAssignment.pon_port_id == PonPort.id)
        .where(
            PonPort.olt_id == olt.id,
            OntAssignment.active.is_(True),
        )
    )
    assignments = list(db.scalars(stmt).all())

    now = datetime.now(UTC)
    updated = 0
    errors = 0

    targets = _build_reading_targets(
        db,
        olt=olt,
        readings=readings,
        assignments=assignments,
    )
    warn_thresh, crit_thresh = get_signal_thresholds(db)
    alert_cooldown_sec = _get_alert_cooldown_seconds(db)
    offline_poll_threshold = _get_offline_poll_threshold(db)
    status_transitions: list[tuple[OntUnit, str, dict]] = []

    for ont, reading in targets:
        try:
            update_values: dict = {}
            if reading.olt_rx_dbm is not None:
                update_values["olt_rx_signal_dbm"] = reading.olt_rx_dbm
            if reading.onu_rx_dbm is not None:
                update_values["onu_rx_signal_dbm"] = reading.onu_rx_dbm
            if reading.distance_m is not None:
                update_values["distance_meters"] = reading.distance_m
            if reading.onu_tx_dbm is not None:
                update_values["onu_tx_signal_dbm"] = reading.onu_tx_dbm
            if reading.temperature_c is not None:
                update_values["ont_temperature_c"] = reading.temperature_c
            if reading.voltage_v is not None:
                update_values["ont_voltage_v"] = reading.voltage_v
            if reading.bias_current_ma is not None:
                update_values["ont_bias_current_ma"] = reading.bias_current_ma

            prev_status = ont.online_status

            if reading.is_online is not None:
                if reading.is_online:
                    update_values["online_status"] = OnuOnlineStatus.online
                    update_values["last_seen_at"] = now
                    update_values["offline_reason"] = None
                    # Reset flap counter when online
                    update_values["consecutive_offline_polls"] = 0
                else:
                    # Flap protection: increment offline counter and only mark offline
                    # after consecutive polls reach threshold
                    new_offline_count = (ont.consecutive_offline_polls or 0) + 1
                    update_values["consecutive_offline_polls"] = new_offline_count

                    if new_offline_count >= offline_poll_threshold:
                        # Threshold reached - mark as offline
                        update_values["online_status"] = OnuOnlineStatus.offline
                        status_val = status_raw.get(reading.onu_index, "")
                        reason = _derive_offline_reason(status_val)
                        if reason is None:
                            update_values["offline_reason"] = None
                        else:
                            try:
                                update_values["offline_reason"] = OnuOfflineReason(reason)
                            except ValueError:
                                update_values["offline_reason"] = OnuOfflineReason.unknown
                    # If threshold not reached, keep current status but track the poll

            # Use SNMP offline_reason OID if available (more precise than status code)
            # Only apply if we're actually transitioning to offline (threshold reached)
            if (
                reading.offline_reason_raw
                and update_values.get("online_status") == OnuOnlineStatus.offline
            ):
                snmp_reason = _derive_offline_reason(reading.offline_reason_raw)
                if snmp_reason:
                    try:
                        update_values["offline_reason"] = OnuOfflineReason(snmp_reason)
                    except ValueError:
                        pass

            # Mark telemetry freshness only when at least one field was observed.
            if update_values:
                resolved_status = resolve_ont_status_snapshot(
                    olt_status=update_values.get("online_status", prev_status),
                    acs_last_inform_at=getattr(ont, "acs_last_inform_at", None),
                    managed=bool(
                        getattr(ont, "tr069_acs_server_id", None)
                        or getattr(ont, "acs_last_inform_at", None)
                    ),
                    now=now,
                    online_window_minutes=resolve_acs_online_window_minutes_for_model(
                        ont
                    ),
                )
                update_values["acs_status"] = resolved_status.acs_status
                update_values["acs_last_inform_at"] = resolved_status.acs_last_inform_at
                update_values["effective_status"] = resolved_status.effective_status
                update_values["effective_status_source"] = (
                    resolved_status.effective_status_source
                )
                update_values["status_resolved_at"] = resolved_status.status_resolved_at
                update_values["signal_updated_at"] = now
            else:
                continue

            db.execute(
                update(OntUnit).where(OntUnit.id == ont.id).values(**update_values)
            )
            updated += 1

            # Track status transitions and signal degradation for events
            new_status = update_values.get("online_status")
            if new_status and prev_status != new_status:
                if new_status == OnuOnlineStatus.offline:
                    reason_val = update_values.get("offline_reason")
                    status_transitions.append(
                        (
                            ont,
                            "offline",
                            {
                                "offline_reason": reason_val.value
                                if reason_val
                                else "unknown",
                            },
                        )
                    )
                elif (
                    new_status == OnuOnlineStatus.online
                    and prev_status == OnuOnlineStatus.offline
                ):
                    status_transitions.append((ont, "online", {}))

            # Signal degradation alerts with cooldown.
            # Only emit when the signal *crosses* a threshold (was OK, now bad)
            # to avoid re-alerting every poll cycle.
            if reading.olt_rx_dbm is not None:
                prev_signal = ont.olt_rx_signal_dbm
                # Cooldown: skip if last update was < 30 min ago and signal
                # was already below threshold (avoids spam on every poll).
                recently_alerted = (
                    ont.signal_updated_at is not None
                    and (
                        now
                        - (
                            ont.signal_updated_at
                            if ont.signal_updated_at.tzinfo is not None
                            else ont.signal_updated_at.replace(tzinfo=UTC)
                        )
                    ).total_seconds()
                    < alert_cooldown_sec
                    and prev_signal is not None
                    and prev_signal < warn_thresh
                )
                if not recently_alerted:
                    if reading.olt_rx_dbm < crit_thresh:
                        if prev_signal is None or prev_signal >= crit_thresh:
                            status_transitions.append(
                                (
                                    ont,
                                    "signal_degraded",
                                    {
                                        "olt_rx_dbm": reading.olt_rx_dbm,
                                        "threshold": crit_thresh,
                                        "severity": "critical",
                                    },
                                )
                            )
                    elif reading.olt_rx_dbm < warn_thresh:
                        if prev_signal is None or prev_signal >= warn_thresh:
                            status_transitions.append(
                                (
                                    ont,
                                    "signal_degraded",
                                    {
                                        "olt_rx_dbm": reading.olt_rx_dbm,
                                        "threshold": warn_thresh,
                                        "severity": "warning",
                                    },
                                )
                            )

                # ±3dB signal delta detection (relative change alert).
                # Fires regardless of absolute threshold — catches sudden
                # fibre events even when signal is still in "good" range.
                if (
                    reading.olt_rx_dbm is not None
                    and prev_signal is not None
                    and not recently_alerted
                ):
                    delta = abs(reading.olt_rx_dbm - prev_signal)
                    if delta >= _DEFAULT_SIGNAL_DELTA_DB:
                        direction = (
                            "drop" if reading.olt_rx_dbm < prev_signal else "rise"
                        )
                        status_transitions.append(
                            (
                                ont,
                                "signal_delta",
                                {
                                    "olt_rx_dbm": reading.olt_rx_dbm,
                                    "previous_dbm": prev_signal,
                                    "delta_db": round(delta, 2),
                                    "direction": direction,
                                    "threshold_db": _DEFAULT_SIGNAL_DELTA_DB,
                                },
                            )
                        )

            # DDM health alerts — temperature, voltage, bias current
            if (
                reading.temperature_c is not None
                and reading.temperature_c > _DDM_TEMPERATURE_WARN_C
            ):
                severity = (
                    "critical"
                    if reading.temperature_c > _DDM_TEMPERATURE_CRIT_C
                    else "warning"
                )
                status_transitions.append(
                    (
                        ont,
                        "ddm_alert",
                        {
                            "metric": "temperature",
                            "value": reading.temperature_c,
                            "unit": "C",
                            "severity": severity,
                        },
                    )
                )
            if reading.voltage_v is not None and (
                reading.voltage_v < _DDM_VOLTAGE_LOW_V
                or reading.voltage_v > _DDM_VOLTAGE_HIGH_V
            ):
                status_transitions.append(
                    (
                        ont,
                        "ddm_alert",
                        {
                            "metric": "voltage",
                            "value": reading.voltage_v,
                            "unit": "V",
                            "severity": "warning",
                        },
                    )
                )
            if (
                reading.bias_current_ma is not None
                and reading.bias_current_ma > _DDM_BIAS_CURRENT_WARN_MA
            ):
                status_transitions.append(
                    (
                        ont,
                        "ddm_alert",
                        {
                            "metric": "bias_current",
                            "value": reading.bias_current_ma,
                            "unit": "mA",
                            "severity": "warning",
                        },
                    )
                )

        except Exception as e:
            logger.error("Error updating ONT %s: %s", ont.id, e)
            errors += 1

    # Emit events for ONT status transitions after bulk update
    for ont, transition, extra in status_transitions:
        try:
            payload = {
                "ont_id": str(ont.id),
                "serial_number": ont.serial_number,
                "olt_id": str(olt.id),
                "olt_name": olt.name,
                **extra,
            }
            if transition == "offline":
                emit_event(db, EventType.ont_offline, payload, actor="system")
            elif transition == "online":
                emit_event(db, EventType.ont_online, payload, actor="system")
            elif transition == "signal_degraded":
                emit_event(db, EventType.ont_signal_degraded, payload, actor="system")
            elif transition == "signal_delta":
                emit_event(db, EventType.ont_signal_delta, payload, actor="system")
            elif transition == "ddm_alert":
                emit_event(db, EventType.ont_ddm_alert, payload, actor="system")
        except Exception as e:
            logger.warning("Failed to emit ONT %s event: %s", transition, e)

    skipped = max(0, polled - len(targets))
    return {"polled": polled, "updated": updated, "errors": errors, "skipped": skipped}


def poll_olt_health(
    olt: OLTDevice,
    *,
    community: str | None = None,
) -> OltHealthReading:
    """Poll OLT hardware health metrics via SNMP.

    Args:
        olt: OLT device to poll.
        community: SNMP community string.

    Returns:
        OltHealthReading with available metrics.
    """
    host = olt.mgmt_ip or olt.hostname
    if not host:
        return OltHealthReading()
    if not community:
        return OltHealthReading()

    vendor = (olt.vendor or "").lower().strip()
    oids: dict[str, str] = {}
    for key, vendor_oids in _OLT_HEALTH_OIDS.items():
        if key in vendor:
            oids = vendor_oids
            break
    if not oids:
        oids = _GENERIC_HEALTH_OIDS

    cpu_raw = _snmpget_value(host, oids["cpu"], community) if "cpu" in oids else None
    temp_raw = (
        _snmpget_value(host, oids["temperature"], community)
        if "temperature" in oids
        else None
    )
    mem_raw = (
        _snmpget_value(host, oids["memory"], community) if "memory" in oids else None
    )
    uptime_raw = (
        _snmpget_value(host, oids["uptime"], community) if "uptime" in oids else None
    )

    cpu = _parse_numeric(cpu_raw)
    temperature = _parse_numeric(temp_raw)
    memory = _parse_numeric(mem_raw)
    uptime = _parse_uptime_ticks(uptime_raw)

    # Clamp percentages to 0-100 range
    if cpu is not None:
        cpu = max(0.0, min(100.0, cpu))
    if memory is not None:
        memory = max(0.0, min(100.0, memory))

    return OltHealthReading(
        cpu_percent=cpu,
        temperature_c=temperature,
        memory_percent=memory,
        uptime_seconds=uptime,
    )


def poll_sfp_modules(
    db: Session,
    olt: OLTDevice,
    *,
    community: str | None = None,
) -> dict[str, int]:
    """Discover and update SFP module optical metrics via SNMP.

    Uses standard entPhysicalTable SFP OIDs for transceiver rx/tx power.
    Updates existing OltSfpModule records matched by card port.

    Returns:
        Stats dict: {discovered, updated, errors}.
    """
    host = olt.mgmt_ip or olt.hostname
    if not host or not community:
        return {"discovered": 0, "updated": 0, "errors": 0}

    # Standard IF-MIB OIDs for transceiver diagnostics (many vendors support these)
    sfp_tx_oid = (
        ".1.3.6.1.4.1.2011.5.25.31.1.1.3.1.9"
        if "huawei" in (olt.vendor or "").lower()
        else ".1.3.6.1.2.1.47.1.1.1.1.7"
    )
    sfp_rx_oid = (
        ".1.3.6.1.4.1.2011.5.25.31.1.1.3.1.10"
        if "huawei" in (olt.vendor or "").lower()
        else ".1.3.6.1.2.1.47.1.1.1.1.7"
    )

    try:
        tx_raw = _parse_snmp_table(
            _run_olt_snmpwalk(host, sfp_tx_oid, community, timeout=20),
            base_oid=sfp_tx_oid,
        )
        rx_raw = _parse_snmp_table(
            _run_olt_snmpwalk(host, sfp_rx_oid, community, timeout=20),
            base_oid=sfp_rx_oid,
        )
    except Exception as e:
        logger.warning("SFP SNMP walk failed for OLT %s: %s", olt.name, e)
        return {"discovered": 0, "updated": 0, "errors": 1}

    if not tx_raw and not rx_raw:
        return {"discovered": 0, "updated": 0, "errors": 0}

    # Update existing SFP module records linked to this OLT's card ports
    sfp_modules = list(
        db.scalars(
            select(OltSfpModule)
            .join(OltCardPort, OltSfpModule.olt_card_port_id == OltCardPort.id)
            .join(OltCard, OltCardPort.card_id == OltCard.id)
            .join(OltShelf, OltCard.shelf_id == OltShelf.id)
            .where(OltCardPort.is_active.is_(True))
            .where(OltShelf.olt_id == olt.id)
        ).all()
    )

    updated_count = 0
    for sfp in sfp_modules:
        port = sfp.olt_card_port
        if not port or port.port_number is None:
            continue
        card = port.card
        slot_number = card.slot_number if card else None
        candidate_indexes = {
            str(port.port_number),
            f"{slot_number}.{port.port_number}" if slot_number is not None else "",
        }
        candidate_indexes.discard("")
        tx_key = next((idx for idx in candidate_indexes if idx in tx_raw), None)
        rx_key = next((idx for idx in candidate_indexes if idx in rx_raw), None)
        tx_val = (
            _parse_signal_value(tx_raw.get(tx_key, ""), 0.01)
            if tx_key is not None
            else None
        )
        rx_val = (
            _parse_signal_value(rx_raw.get(rx_key, ""), 0.01)
            if rx_key is not None
            else None
        )
        if tx_val is not None:
            sfp.tx_power_dbm = tx_val
            updated_count += 1
        if rx_val is not None:
            sfp.rx_power_dbm = rx_val

    if updated_count:
        db.flush()

    return {
        "discovered": len(tx_raw) + len(rx_raw),
        "updated": updated_count,
        "errors": 0,
    }


def _ping_host(host: str, count: int = 3, timeout: int = 5) -> bool:
    """Ping a host and return True if reachable.

    Uses 3 pings with 5 second timeout each to handle high-latency links.
    """
    import subprocess

    ping_binary = shutil.which("ping")
    if not ping_binary:
        logger.warning("ping executable not found while probing host %s", host)
        return False

    try:
        result = subprocess.run(
            [ping_binary, "-c", str(count), "-W", str(timeout), host],
            capture_output=True,
            timeout=timeout * count + 5,
        )
        return result.returncode == 0
    except Exception:
        return False


def poll_single_olt_device(db: Session, olt_id: str) -> dict[str, int | str]:
    """Poll a single OLT device for ONT signal levels and OLT health.

    This function is designed to be called in parallel by separate Celery tasks.
    Each invocation handles its own database transaction.

    Args:
        db: Database session (caller manages transaction).
        olt_id: UUID string of the OLT device to poll.

    Returns:
        Stats dict: {olt_name, polled, updated, errors, health_collected, sfp_updated}.
    """
    import subprocess
    from uuid import UUID

    olt = db.get(OLTDevice, UUID(olt_id))
    if not olt:
        logger.warning("OLT %s not found for polling", olt_id)
        return {"olt_name": "unknown", "polled": 0, "updated": 0, "errors": 1}

    if not olt.is_active:
        logger.info("Skipping inactive OLT %s", olt.name)
        return {"olt_name": olt.name, "polled": 0, "updated": 0, "errors": 0, "skipped": 1}

    result: dict[str, int | str] = {
        "olt_name": olt.name,
        "polled": 0,
        "updated": 0,
        "errors": 0,
        "health_collected": 0,
        "sfp_updated": 0,
    }

    # Ping check first - updates reachability status
    host = olt.mgmt_ip or olt.hostname
    if host:
        ping_ok = _ping_host(host)
        olt.last_ping_at = datetime.now(UTC)
        olt.last_ping_ok = ping_ok
        if not ping_ok:
            logger.warning("OLT %s (%s) not reachable via ping", olt.name, host)
            olt.last_poll_status = PollStatus.failed
            olt.last_poll_error = "Ping failed - host unreachable"
            olt.consecutive_poll_failures = (olt.consecutive_poll_failures or 0) + 1
            olt.last_poll_at = datetime.now(UTC)
            db.commit()
            return {
                "olt_name": olt.name,
                "polled": 0,
                "updated": 0,
                "errors": 1,
                "ping_failed": 1,
            }

    snmp_cfg = _get_olt_snmp_config(db, olt)
    community = (
        str(snmp_cfg.get("community")).strip()
        if snmp_cfg.get("community") is not None
        else None
    )

    # Track polling status for reachability
    poll_failed = False
    poll_error_msg: str | None = None
    is_timeout = False

    # Poll ONT signals
    try:
        poll_result = poll_olt_ont_signals(db, olt, community=community)
        result["polled"] = poll_result["polled"]
        result["updated"] = poll_result["updated"]
        result["errors"] = poll_result["errors"]
    except subprocess.TimeoutExpired as e:
        logger.error("Failed to poll OLT %s ONT signals: %s", olt.name, e)
        result["errors"] = 1
        poll_failed = True
        is_timeout = True
        poll_error_msg = f"SNMP timeout after {getattr(e, 'timeout', 90)}s"
    except Exception as e:
        logger.error("Failed to poll OLT %s ONT signals: %s", olt.name, e)
        result["errors"] = 1
        poll_failed = True
        poll_error_msg = str(e)[:500]

    # Poll OLT hardware health
    health: OltHealthReading | None = None
    try:
        health = poll_olt_health(olt, community=community)
        if health:
            result["health_collected"] = 1
    except Exception as e:
        logger.error("Failed to poll health for OLT %s: %s", olt.name, e)
        result["errors"] = int(result.get("errors", 0)) + 1

    # Auto-detect firmware/software version via sysDescr
    if community and olt.mgmt_ip:
        try:
            sys_descr = _snmpget_value(olt.mgmt_ip, _SYSDESCR_OID, community)
            if sys_descr:
                fw, sw = _parse_sysdescr(sys_descr, olt.vendor or "")
                if sw and sw != olt.software_version:
                    logger.debug(
                        "Auto-detected software_version for OLT %s: %s",
                        olt.name,
                        sw,
                    )
                    olt.software_version = sw
                if fw and fw != olt.firmware_version:
                    logger.debug(
                        "Auto-detected firmware_version for OLT %s: %s",
                        olt.name,
                        fw,
                    )
                    olt.firmware_version = fw
        except Exception as e:
            logger.debug("sysDescr fetch failed for OLT %s: %s", olt.name, e)

    # Poll SFP module metrics
    try:
        sfp_result = poll_sfp_modules(db, olt, community=community)
        result["sfp_updated"] = sfp_result["updated"]
    except Exception as e:
        logger.error("Failed to poll SFP modules for OLT %s: %s", olt.name, e)
        result["errors"] = int(result.get("errors", 0)) + 1

    # Update OLT polling status for reachability tracking
    olt.last_poll_at = datetime.now(UTC)
    if poll_failed:
        olt.last_poll_status = PollStatus.timeout if is_timeout else PollStatus.failed
        olt.last_poll_error = poll_error_msg
        olt.consecutive_poll_failures = (olt.consecutive_poll_failures or 0) + 1
    else:
        olt.last_poll_status = PollStatus.success
        olt.last_poll_error = None
        olt.consecutive_poll_failures = 0

    db.commit()

    # Push health metrics for this OLT
    if health:
        try:
            _push_olt_health_metrics({olt.name: health})
        except Exception as e:
            logger.error("OLT %s health metrics push failed: %s", olt.name, e)

    logger.info(
        "OLT %s polling complete: %d ONTs polled, %d updated, %d errors",
        olt.name,
        result["polled"],
        result["updated"],
        result["errors"],
    )
    return result


def reconcile_snmp_status_with_signal(
    *,
    vendor: str,
    raw_status: str | None,
    olt_rx_dbm: float | None,
) -> tuple[OnuOnlineStatus, OnuOfflineReason | None, bool]:
    """Determine effective ONT status using SNMP state and signal level.

    Args:
        vendor: OLT vendor key (e.g. "huawei", "zte").
        raw_status: Raw SNMP online/offline state string.
        olt_rx_dbm: OLT-side received signal in dBm.

    Returns:
        Tuple of (status, offline_reason, was_reconciled).
    """
    if raw_status is None:
        return OnuOnlineStatus.unknown, None, False

    state_lower = raw_status.lower().strip()

    # Map SNMP states to status
    if state_lower in ("online", "up", "1", "active"):
        return OnuOnlineStatus.online, None, False

    if state_lower in ("offline", "down", "0", "inactive", "2"):
        # If SNMP says offline but we have a valid signal, something is off
        if olt_rx_dbm is not None and -30.0 < olt_rx_dbm < 0.0:
            logger.warning(
                "SNMP reports offline but has valid signal %.1f dBm (vendor=%s)",
                olt_rx_dbm,
                vendor,
            )
            # Reconcile: trust signal over SNMP state
            return OnuOnlineStatus.online, None, True
        return OnuOnlineStatus.offline, OnuOfflineReason.unknown, False

    # Handle vendor-specific offline reasons
    if "los" in state_lower or "loss" in state_lower:
        return OnuOnlineStatus.offline, OnuOfflineReason.los, False
    if "dying" in state_lower:
        return OnuOnlineStatus.offline, OnuOfflineReason.dying_gasp, False

    return OnuOnlineStatus.unknown, None, False
