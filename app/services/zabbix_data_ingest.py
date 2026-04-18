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
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntUnit
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
    # ONT online status
    "status": [
        "gpon.ont.status",
        "ont.online.status",
        "huawei.gpon.ont.state",
    ],
}


def _get_client() -> ZabbixClient:
    """Get Zabbix client from environment."""
    return ZabbixClient.from_env()


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


def ingest_olt_signal_data(
    db: Session,
    olt: OLTDevice,
    client: ZabbixClient | None = None,
) -> int:
    """Ingest ONT signal data from Zabbix for a single OLT.

    Fetches latest item values from Zabbix for this OLT's host,
    parses the ONT identifiers, and updates corresponding OntUnit records.

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
        # Get all items for this OLT host
        items = client.get_items(
            host_ids=[olt.zabbix_host_id],
            metric="gpon",  # Filter to GPON-related items
            limit=10000,
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

    # Group items by ONT identifier
    ont_data: dict[tuple[str, str], dict[str, float]] = {}
    now = datetime.now(UTC)

    for item in items:
        item_key = item.get("key_", "")
        last_value = item.get("lastvalue")
        last_clock = item.get("lastclock")

        # Skip if no recent value
        if not last_value or last_value in ("", "0"):
            continue

        # Extract ONT identifier
        pon_port, ont_index = _extract_ont_identifier(item_key)
        if not pon_port or not ont_index:
            continue

        # Identify metric type
        metric_type = _identify_metric_type(item_key)
        if not metric_type:
            continue

        try:
            value = float(last_value)
        except ValueError:
            continue

        key = (pon_port, ont_index)
        if key not in ont_data:
            ont_data[key] = {}
        ont_data[key][metric_type] = value

    if not ont_data:
        logger.debug("olt_no_ont_data", extra={"olt_id": str(olt.id)})
        return 0

    # Update OntUnit records
    updated_count = 0
    for (pon_port, ont_index), metrics in ont_data.items():
        # Find matching OntUnit
        # Try matching by pon_port_name and onu_id
        stmt = (
            select(OntUnit)
            .where(OntUnit.olt_device_id == olt.id)
            .where(OntUnit.is_active.is_(True))
        )

        # Try to match on onu_id if available
        try:
            onu_id = int(ont_index)
            stmt = stmt.where(OntUnit.onu_id == onu_id)
        except ValueError:
            continue

        ont = db.scalars(stmt).first()
        if not ont:
            continue

        # Update signal fields
        changed = False
        if "olt_rx" in metrics:
            ont.olt_rx_signal_dbm = metrics["olt_rx"]
            changed = True
        if "onu_rx" in metrics:
            ont.onu_rx_signal_dbm = metrics["onu_rx"]
            changed = True
        if "onu_tx" in metrics:
            ont.onu_tx_signal_dbm = metrics["onu_tx"]
            changed = True

        if changed:
            ont.signal_updated_at = now
            updated_count += 1

    db.flush()
    logger.info(
        "olt_signal_ingest_complete",
        extra={
            "olt_id": str(olt.id),
            "onts_updated": updated_count,
            "items_processed": len(ont_data),
        },
    )
    return updated_count


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
