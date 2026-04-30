"""Ingest monitoring data from Zabbix into DotMac database.

This module pulls signal and status data collected by Zabbix SNMP monitoring
and updates the corresponding device records in the DotMac database.

Data flow:
    Zabbix SNMP polling → Zabbix DB → (this module) → DotMac DB
    → olt_polling_metrics.py → VictoriaMetrics → ont_metrics adapter

This replaces direct SNMP polling with Zabbix-mediated data collection.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntUnit, OnuOfflineReason, OnuOnlineStatus
from app.services.zabbix import ZabbixClient, ZabbixClientError

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    """Result of a data ingest operation."""

    olts_processed: int = 0
    onts_updated: int = 0
    errors: list[str] = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


# Zabbix item key patterns for OLT/ONT signal metrics
# These should match the items configured in Zabbix OLT templates
ITEM_KEY_PATTERNS = {
    # OLT-side receive power (OLT sees this from ONT)
    "olt_rx": [
        "gpon.ont.rx.power",
        "ont.signal.olt_rx",
        "olt.rx.power",
        "huawei.gpon.ont.rx",
    ],
    # ONT-side receive power (ONT reports this)
    "onu_rx": [
        "gpon.onu.rx.power",
        "ont.signal.onu_rx",
        "onu.rx.power",
        "huawei.gpon.onu.rx",
    ],
    # ONT-side transmit power
    "onu_tx": [
        "gpon.onu.tx.power",
        "ont.signal.onu_tx",
        "onu.tx.power",
    ],
    # ONT OLT status
    "status": [
        "gpon.ont.status",
        "ont.online.status",
        "huawei.gpon.ont.state",
    ],
}


def _get_client() -> ZabbixClient:
    """Get Zabbix client from environment."""
    return ZabbixClient.from_env()


def _decode_huawei_pon_index(encoded: int) -> tuple[str, int]:
    """Decode Huawei PON port index from encoded SNMP ifIndex.

    For Huawei MA5680T and similar OLTs, the ifIndex encoding is:
    ifIndex = 4194304000 + (snmp_slot * 2048) + (port * 256)

    SNMP slot numbers have an offset from physical slot numbers:
    - SNMP slot 8 = Physical slot 2 (offset of 6)
    - This is because SNMP slots 0-7 are reserved for system boards

    Args:
        encoded: The encoded ifIndex from SNMP OID (e.g., 4194320384)

    Returns:
        Tuple of (port_string like "0/2/1", encoded_value)
    """
    base = 4194304000
    snmp_slot_offset = 6  # SNMP slot = physical slot + 6

    offset = encoded - base
    snmp_slot = offset // 2048
    port = (offset % 2048) // 256

    # Convert SNMP slot to physical slot
    physical_slot = snmp_slot - snmp_slot_offset

    # Frame is always 0 for these OLTs
    return f"0/{physical_slot}/{port}", encoded


def _parse_snmp_walk(walk_data: str) -> list[tuple[str, int, int | float]]:
    """Parse raw SNMP walk output into structured data.

    Args:
        walk_data: Raw SNMP walk output from Zabbix

    Returns:
        List of (pon_port, ont_index, value) tuples
    """
    results = []
    # Pattern matches: .OID.ifIndex.ont_idx = TYPE: value
    # OID examples:
    #   .1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4.4194320384.0 = INTEGER: -2318
    pattern = re.compile(
        r"\.(\d+)\.(\d+)\s*=\s*(?:INTEGER|Gauge32|Counter32|Opaque):\s*([-\d.]+)"
    )

    for line in walk_data.strip().split("\n"):
        match = pattern.search(line)
        if match:
            ifindex = int(match.group(1))
            ont_index = int(match.group(2))
            value = float(match.group(3))

            # Only process valid Huawei ifIndex values (base is 4194304000)
            if ifindex >= 4194304000:
                pon_port, _ = _decode_huawei_pon_index(ifindex)
                results.append((pon_port, ont_index, value))

    return results


def _extract_ont_identifier(item_key: str) -> tuple[str | None, str | None]:
    """Extract PON port and ONT index from Zabbix item key.

    Zabbix LLD items typically have keys like:
    - gpon.ont.rx.power[0/0/1,5] -> port=0/0/1, ont=5
    - huawei.gpon.ont.rx[gpon0/0/3,12] -> port=gpon0/0/3, ont=12

    Returns:
        Tuple of (pon_port, ont_index) or (None, None) if not parseable.
    """
    # Try bracket notation: key[port,ont]
    match = re.search(r"\[([^,\]]+),\s*(\d+)\]", item_key)
    if match:
        return match.group(1), match.group(2)

    # Try alternate format: key.port.ont
    match = re.search(r"\.(\d+/\d+/\d+)\.(\d+)$", item_key)
    if match:
        return match.group(1), match.group(2)

    return None, None


def _identify_metric_type(item_key: str) -> str | None:
    """Identify the metric type from a Zabbix item key."""
    key_lower = item_key.lower()
    for metric_type, patterns in ITEM_KEY_PATTERNS.items():
        for pattern in patterns:
            if pattern.lower() in key_lower:
                return metric_type
    return None


def _parse_status_code(value: float) -> tuple[OnuOnlineStatus, OnuOfflineReason | None]:
    """Parse SNMP status code to ONT status and offline reason.

    Huawei status codes:
        1: online
        2: offline (unknown reason)
        3: offline (power fail)
        4: offline (LOS - loss of signal)
        5: offline (dying gasp)
    """
    code = int(value)
    if code == 1:
        return OnuOnlineStatus.online, None
    if code == 2:
        return OnuOnlineStatus.offline, OnuOfflineReason.unknown
    if code == 3:
        return OnuOnlineStatus.offline, OnuOfflineReason.power_fail
    if code == 4:
        return OnuOnlineStatus.offline, OnuOfflineReason.los
    if code == 5:
        return OnuOnlineStatus.offline, OnuOfflineReason.dying_gasp
    return OnuOnlineStatus.offline, None


def ingest_olt_signal_data(
    db: Session,
    olt: OLTDevice,
    client: ZabbixClient | None = None,
) -> int:
    """Ingest ONT signal data from Zabbix for a single OLT.

    Fetches raw SNMP walk items from Zabbix, parses the ONT data,
    and updates corresponding OntUnit records.

    Args:
        db: Database session
        olt: OLT device to fetch data for
        client: Optional Zabbix client (creates one if not provided)

    Returns:
        Number of ONT records updated
    """
    if client is None:
        client = _get_client()

    if not olt.zabbix_host_id:
        logger.debug("olt_no_zabbix_host", extra={"olt_id": str(olt.id)})
        return 0

    try:
        # Get walk items for this OLT host
        items = client.get_items(
            host_ids=[olt.zabbix_host_id],
            metric="walk",  # Get raw walk items
            limit=100,
        )
    except ZabbixClientError as exc:
        logger.error(
            "zabbix_items_fetch_failed",
            extra={"olt_id": str(olt.id), "error": str(exc)},
        )
        return 0

    if not items:
        logger.debug("olt_no_items", extra={"olt_id": str(olt.id)})
        return 0

    # Parse walk items to extract per-ONT data
    ont_data: dict[tuple[str, int], dict[str, float]] = {}
    now = datetime.now(UTC)

    for item in items:
        item_key = item.get("key_", "")
        last_value = item.get("lastvalue", "")

        if not last_value:
            continue

        # Determine metric type from item key
        if "opt.rx" in item_key or "rx.walk" in item_key.lower():
            metric_type = "olt_rx"
        elif "ont.status" in item_key or "status.walk" in item_key.lower():
            metric_type = "status"
        else:
            continue

        # Parse the raw SNMP walk data
        parsed = _parse_snmp_walk(last_value)
        for pon_port, ont_index, value in parsed:
            key = (pon_port, ont_index)
            if key not in ont_data:
                ont_data[key] = {}

            if metric_type == "olt_rx":
                ont_data[key]["_saw_olt_rx"] = True
                # Signal values are in 0.01 dBm units, convert to dBm
                dbm_value = value / 100.0
                # Filter out invalid values (0x7FFFFFFF/100 = 21474836.47 means no signal)
                # Valid optical signal range is roughly -45 to +5 dBm
                if -50 < dbm_value < 10:
                    ont_data[key]["olt_rx"] = dbm_value
            elif metric_type == "status":
                ont_data[key]["status"] = value

    if not ont_data:
        logger.debug("olt_no_ont_data", extra={"olt_id": str(olt.id)})
        return 0

    from app.services.network.ont_status import apply_olt_status_observation

    # Build mapping from poll data keys to external_ids
    key_to_external_id: dict[tuple[str, int], str] = {}
    for pon_port, ont_index in ont_data.keys():
        normalized_port = pon_port.lower().replace("gpon", "").strip()
        key_to_external_id[(pon_port, ont_index)] = f"{normalized_port}.{ont_index}"

    poll_external_ids = set(key_to_external_id.values())

    # Single query: get all active ONTs on this OLT that are either:
    # - previously online (to detect missing), OR
    # - in the current poll data (to update)
    all_relevant_stmt = (
        select(OntUnit)
        .where(OntUnit.olt_device_id == olt.id)
        .where(OntUnit.is_active.is_(True))
        .where(
            (OntUnit.olt_status == OnuOnlineStatus.online)
            | (OntUnit.external_id.in_(poll_external_ids))
        )
    )
    all_relevant = {ont.external_id: ont for ont in db.scalars(all_relevant_stmt).all()}

    # Track which previously-online ONTs we see in this poll
    previously_online_ids = {
        eid for eid, ont in all_relevant.items()
        if ont.olt_status == OnuOnlineStatus.online
    }

    # Update OntUnit records
    updated_count = 0
    offline_count = 0
    for key, metrics in ont_data.items():
        external_id = key_to_external_id[key]
        ont = all_relevant.get(external_id)
        if not ont:
            continue

        # Check explicit status from SNMP walk
        polled_status = None
        if "status" in metrics:
            polled_status, offline_reason = _parse_status_code(metrics["status"])
            if polled_status == OnuOnlineStatus.offline:
                apply_olt_status_observation(ont, polled_status, offline_reason, now=now)
                ont.signal_updated_at = now
                offline_count += 1
                continue  # Don't process signal data for offline ONTs

        # Update signal fields
        if metrics.get("_saw_olt_rx") and "olt_rx" not in metrics:
            if ont.olt_rx_signal_dbm is not None:
                ont.olt_rx_signal_dbm = None
        if "olt_rx" in metrics:
            ont.olt_rx_signal_dbm = metrics["olt_rx"]
        if "onu_rx" in metrics:
            ont.onu_rx_signal_dbm = metrics["onu_rx"]
        if "onu_tx" in metrics:
            ont.onu_tx_signal_dbm = metrics["onu_tx"]

        # ONT appeared in poll - mark online and refresh timestamp
        # Trust explicit status=online OR presence of signal data
        has_signal = any(k in metrics for k in ("olt_rx", "onu_rx", "onu_tx", "_saw_olt_rx"))
        if polled_status == OnuOnlineStatus.online or has_signal:
            apply_olt_status_observation(ont, OnuOnlineStatus.online, now=now)
            ont.signal_updated_at = now
            updated_count += 1

    # Mark previously-online ONTs as offline if they weren't seen in this poll
    missing_count = 0
    for external_id in previously_online_ids:
        if external_id not in poll_external_ids:
            ont = all_relevant[external_id]
            apply_olt_status_observation(ont, OnuOnlineStatus.offline, OnuOfflineReason.los, now=now)
            ont.signal_updated_at = now
            missing_count += 1

    db.flush()
    logger.info(
        "olt_signal_ingest_complete",
        extra={
            "olt_id": str(olt.id),
            "onts_online": updated_count,
            "onts_offline": offline_count,
            "onts_missing": missing_count,
            "items_processed": len(ont_data),
        },
    )
    return updated_count + offline_count + missing_count


def ingest_all_olt_signals(db: Session) -> IngestResult:
    """Ingest ONT signal data from Zabbix for all OLTs.

    Iterates through all active OLTs with Zabbix hosts and pulls
    their latest signal data.

    Returns:
        IngestResult with counts and any errors.
    """
    result = IngestResult()
    client = _get_client()

    # Get all active OLTs with Zabbix host IDs
    stmt = select(OLTDevice).where(
        OLTDevice.is_active.is_(True),
        OLTDevice.zabbix_host_id.isnot(None),
    )
    olts = db.scalars(stmt).all()

    for olt in olts:
        try:
            updated = ingest_olt_signal_data(db, olt, client=client)
            result.olts_processed += 1
            result.onts_updated += updated
        except Exception as exc:
            error_msg = f"{olt.name}: {exc}"
            result.errors.append(error_msg)
            logger.exception(
                "olt_ingest_exception",
                extra={"olt_id": str(olt.id)},
            )

    logger.info(
        "signal_ingest_complete",
        extra={
            "olts_processed": result.olts_processed,
            "onts_updated": result.onts_updated,
            "errors": len(result.errors),
        },
    )
    return result


# Note: OLT health ingest (CPU, memory, temperature, uptime) would require
# adding fields to OLTDevice model. The current implementation focuses on
# ONT signal data which is stored in OntUnit records.
