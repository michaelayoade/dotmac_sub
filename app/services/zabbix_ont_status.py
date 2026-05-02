"""Read ONT status and optical signal data directly from Zabbix."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.services.zabbix import ZabbixClient, ZabbixClientError, zabbix_configured

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.network import OLTDevice, OntUnit

logger = logging.getLogger(__name__)

_WALK_ENTRY_RE = re.compile(
    r"\.(\d+)\.(\d+)\s*=\s*(?:INTEGER|Gauge32|Counter32|Opaque):\s*([-\d.]+)"
)
_EXTERNAL_ID_RE = re.compile(r"0/(\d+)/(\d+)\.(\d+)")
_HUAWEI_EXTERNAL_ID_RE = re.compile(r"huawei:(\d+)\.(\d+)", re.IGNORECASE)
_HUAWEI_IFINDEX_BASE = 4194304000


@dataclass(frozen=True)
class OntSignalData:
    """Current ONT link state from Zabbix.

    Monitoring status is intentionally binary for the DotMac UI: every ONT is
    either online or offline. Zabbix/API errors are represented as offline with
    an error string so callers can log or surface diagnostics without adding a
    third status.
    """

    online: bool
    olt_rx_dbm: float | None = None
    onu_rx_dbm: float | None = None
    updated_at: datetime | None = None
    error: str | None = None

    @property
    def status(self) -> str:
        return "online" if self.online else "offline"

    @property
    def signal_quality(self) -> str:
        if self.olt_rx_dbm is None:
            return "offline"
        if self.olt_rx_dbm >= -25:
            return "good"
        if self.olt_rx_dbm >= -28:
            return "warning"
        return "critical"


def _offline(error: str | None = None) -> OntSignalData:
    return OntSignalData(online=False, error=error)


def _decode_huawei_ifindex(encoded: int) -> tuple[int, int] | None:
    if encoded < _HUAWEI_IFINDEX_BASE:
        return None
    offset = encoded - _HUAWEI_IFINDEX_BASE
    snmp_slot = offset // 2048
    port = (offset % 2048) // 256
    return snmp_slot, port


def _parse_external_id(value: str | None) -> tuple[int, int, int] | None:
    normalized = str(value or "").strip()
    huawei_match = _HUAWEI_EXTERNAL_ID_RE.fullmatch(normalized)
    if huawei_match:
        decoded = _decode_huawei_ifindex(int(huawei_match.group(1)))
        if decoded is None:
            return None
        snmp_slot, port = decoded
        return snmp_slot, port, int(huawei_match.group(2))

    match = _EXTERNAL_ID_RE.fullmatch(normalized)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _parse_ont_target(ont: OntUnit) -> tuple[int, int, int] | None:
    parsed = _parse_external_id(getattr(ont, "external_id", None))
    if parsed is not None:
        return parsed

    external_id = str(getattr(ont, "external_id", "") or "").strip()
    if not external_id.isdigit():
        return None

    board = str(getattr(ont, "board", "") or "").strip()
    port = str(getattr(ont, "port", "") or "").strip()
    if not port.isdigit():
        return None

    board_parts = [part for part in board.split("/") if part != ""]
    if not board_parts or not board_parts[-1].isdigit():
        return None

    return int(board_parts[-1]), int(port), int(external_id)


def _parse_walk_entries(walk_data: str) -> list[tuple[int, int, int, float]]:
    entries: list[tuple[int, int, int, float]] = []
    for line in str(walk_data or "").splitlines():
        match = _WALK_ENTRY_RE.search(line)
        if not match:
            continue
        decoded = _decode_huawei_ifindex(int(match.group(1)))
        if decoded is None:
            continue
        snmp_slot, port = decoded
        entries.append((snmp_slot, port, int(match.group(2)), float(match.group(3))))
    return entries


def _item_timestamp(item: dict) -> datetime | None:
    try:
        lastclock = int(item.get("lastclock") or 0)
    except (TypeError, ValueError):
        return None
    if lastclock <= 0:
        return None
    return datetime.fromtimestamp(lastclock, tz=UTC)


def _is_rx_item(item_key: str) -> bool:
    key = item_key.lower()
    return "opt.rx" in key or "rx.walk" in key or "ont.rx" in key


def _is_onu_rx_item(item_key: str) -> bool:
    key = item_key.lower()
    return "onu.rx" in key or "ont.onu.rx" in key


def _is_status_item(item_key: str) -> bool:
    key = item_key.lower()
    return "ont.status" in key or "status.walk" in key or "ont.state" in key


def _normalize_signal(raw_value: float) -> float | None:
    dbm = raw_value / 100.0
    if -50 < dbm < 10:
        return dbm
    return None


def _parse_status_code(raw_value: float) -> bool:
    # Huawei GPON ONT state convention used by the existing ingest path:
    # 1=online; everything else is offline for DotMac monitoring.
    return int(raw_value) == 1


def _detect_slot_offset(
    entries: list[tuple[int, int, int, float]],
    targets: dict[str, tuple[int, int, int]],
) -> int | None:
    votes: dict[int, int] = {}
    target_values = set(targets.values())
    for snmp_slot, port, ont_index, _value in entries:
        for target_slot, target_port, target_ont_index in target_values:
            if port == target_port and ont_index == target_ont_index:
                offset = snmp_slot - target_slot
                votes[offset] = votes.get(offset, 0) + 1
    if not votes:
        return None
    return max(votes.items(), key=lambda item: item[1])[0]


def get_olt_ont_snapshot_from_zabbix(
    olt: OLTDevice,
    onts: list[OntUnit],
    *,
    client: ZabbixClient | None = None,
) -> dict[str, OntSignalData]:
    """Return a binary online/offline Zabbix snapshot for ONTs on one OLT."""
    targets = {
        str(ont.id): parsed
        for ont in onts
        if getattr(ont, "id", None)
        for parsed in [_parse_ont_target(ont)]
        if parsed is not None
    }
    result = {str(getattr(ont, "id", "")): _offline("not in Zabbix") for ont in onts}
    if not targets:
        return result
    if not zabbix_configured():
        return {ont_id: _offline("Zabbix not configured") for ont_id in result}
    if not getattr(olt, "zabbix_host_id", None):
        return {ont_id: _offline("OLT not linked to Zabbix") for ont_id in result}

    try:
        zbx = client or ZabbixClient.from_env()
        items = zbx.get_items(host_ids=[olt.zabbix_host_id], metric="walk", limit=100)
    except ZabbixClientError as exc:
        logger.warning("zabbix_ont_snapshot_failed", extra={"error": str(exc)})
        return {ont_id: _offline(str(exc)) for ont_id in result}

    parsed_items: list[tuple[dict, list[tuple[int, int, int, float]]]] = []
    slot_offset: int | None = None
    for item in items:
        entries = _parse_walk_entries(str(item.get("lastvalue") or ""))
        if not entries:
            continue
        parsed_items.append((item, entries))
        if slot_offset is None:
            slot_offset = _detect_slot_offset(entries, targets)

    if slot_offset is None:
        return {ont_id: _offline("ONT not found in Zabbix") for ont_id in result}

    by_tuple = {target: ont_id for ont_id, target in targets.items()}
    values: dict[str, dict[str, object]] = {
        ont_id: {"online": False, "error": None} for ont_id in targets
    }

    for item, entries in parsed_items:
        item_key = str(item.get("key_") or "")
        timestamp = _item_timestamp(item)
        is_rx = _is_rx_item(item_key)
        is_onu_rx = _is_onu_rx_item(item_key)
        is_status = _is_status_item(item_key)
        if not (is_rx or is_status):
            continue

        for snmp_slot, port, ont_index, raw_value in entries:
            physical_slot = snmp_slot - slot_offset
            ont_id = by_tuple.get((physical_slot, port, ont_index))
            if ont_id is None:
                continue
            current = values[ont_id]
            if timestamp is not None:
                previous = current.get("updated_at")
                if previous is None or timestamp > previous:
                    current["updated_at"] = timestamp
            if is_status:
                current["online"] = _parse_status_code(raw_value)
            if is_rx:
                dbm = _normalize_signal(raw_value)
                if dbm is None:
                    continue
                if is_onu_rx:
                    current["onu_rx_dbm"] = dbm
                else:
                    current["olt_rx_dbm"] = dbm
                current["online"] = True

    for ont_id, current in values.items():
        result[ont_id] = OntSignalData(
            online=bool(current.get("online")),
            olt_rx_dbm=current.get("olt_rx_dbm"),  # type: ignore[arg-type]
            onu_rx_dbm=current.get("onu_rx_dbm"),  # type: ignore[arg-type]
            updated_at=current.get("updated_at"),  # type: ignore[arg-type]
            error=current.get("error"),  # type: ignore[arg-type]
        )
    return result


def get_ont_signal_from_zabbix(ont: OntUnit) -> OntSignalData:
    """Fetch current binary ONT status/signal data directly from Zabbix."""
    olt = getattr(ont, "olt_device", None)
    if olt is None:
        olt = getattr(ont, "olt", None)
    if olt is None:
        return _offline("OLT not linked to ONT")
    return get_olt_ont_snapshot_from_zabbix(olt, [ont]).get(str(ont.id), _offline())


def get_ont_snapshots_from_zabbix(
    db: Session,
    onts: Sequence[OntUnit],
) -> dict[str, OntSignalData]:
    """Return Zabbix snapshots for ONTs grouped by their OLT."""
    from app.models.network import OLTDevice

    result: dict[str, OntSignalData] = {
        str(getattr(ont, "id", "")): _offline("OLT not linked to ONT") for ont in onts
    }
    onts_by_olt_id: dict[object, list[OntUnit]] = {}
    for ont in onts:
        ont_id = str(getattr(ont, "id", ""))
        if not ont_id:
            continue
        olt_id = getattr(ont, "olt_device_id", None)
        if olt_id is None:
            continue
        onts_by_olt_id.setdefault(olt_id, []).append(ont)

    for olt_id, olt_onts in onts_by_olt_id.items():
        olt = db.get(OLTDevice, olt_id)
        if olt is None:
            continue
        result.update(get_olt_ont_snapshot_from_zabbix(olt, olt_onts))
    return result


def get_olt_ont_summary_from_zabbix(
    olt: OLTDevice,
    onts: list[OntUnit] | None = None,
) -> dict[str, int | str]:
    """Get online/offline ONT counts directly from Zabbix."""
    if not zabbix_configured() or not getattr(olt, "zabbix_host_id", None):
        return {
            "online_count": 0,
            "offline_count": len(onts or []),
            "total_count": len(onts or []),
            "low_signal_count": 0,
            "error": "Zabbix not configured for this OLT",
        }

    try:
        client = ZabbixClient.from_env()
        items = client.get_items(host_ids=[olt.zabbix_host_id], metric="ont.count", limit=10)
    except ZabbixClientError as exc:
        if onts is not None:
            snapshot = get_olt_ont_snapshot_from_zabbix(olt, onts)
            online = sum(1 for item in snapshot.values() if item.online)
            low_signal = sum(
                1
                for item in snapshot.values()
                if item.olt_rx_dbm is not None and item.olt_rx_dbm < -25
            )
            return {
                "online_count": online,
                "offline_count": len(snapshot) - online,
                "total_count": len(snapshot),
                "low_signal_count": low_signal,
                "error": str(exc),
            }
        return {
            "online_count": 0,
            "offline_count": 0,
            "total_count": 0,
            "low_signal_count": 0,
            "error": str(exc),
        }

    result = {
        "online_count": 0,
        "offline_count": 0,
        "total_count": 0,
        "low_signal_count": 0,
    }
    for item in items:
        key = str(item.get("key_") or "")
        try:
            value = int(float(item.get("lastvalue") or 0))
        except (TypeError, ValueError):
            value = 0
        if "ont.count.online" in key:
            result["online_count"] = value
        elif "ont.count.offline" in key:
            result["offline_count"] = value
        elif "ont.count.total" in key:
            result["total_count"] = value
        elif "ont.low.signal" in key:
            result["low_signal_count"] = value

    if result["total_count"] <= 0 and onts is not None:
        snapshot = get_olt_ont_snapshot_from_zabbix(olt, onts, client=client)
        online = sum(1 for item in snapshot.values() if item.online)
        low_signal = sum(
            1
            for item in snapshot.values()
            if item.olt_rx_dbm is not None and item.olt_rx_dbm < -25
        )
        result["online_count"] = online
        result["offline_count"] = len(snapshot) - online
        result["total_count"] = len(snapshot)
        result["low_signal_count"] = low_signal
    elif result["total_count"] <= 0:
        result["total_count"] = result["online_count"] + result["offline_count"]
    return result
