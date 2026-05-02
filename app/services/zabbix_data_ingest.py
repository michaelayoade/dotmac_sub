"""Persist ONT status and optical signal data collected by Zabbix."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntUnit, OnuOfflineReason, OnuOnlineStatus
from app.services.network.ont_status import apply_olt_status_observation
from app.services.zabbix import ZabbixClient, ZabbixClientError

logger = logging.getLogger(__name__)

_WALK_ENTRY_RE = re.compile(
    r"\.(\d+)\.(\d+)\s*=\s*(?:INTEGER|Gauge32|Counter32|Opaque):\s*([-\d.]+)"
)
_EXTERNAL_ID_RE = re.compile(r"0/(\d+)/(\d+)\.(\d+)")
_HUAWEI_EXTERNAL_ID_RE = re.compile(r"huawei:(\d+)\.(\d+)", re.IGNORECASE)
_HUAWEI_IFINDEX_BASE = 4194304000
_HUAWEI_IFINDEX_SLOT_STRIDE = 8192
_HUAWEI_IFINDEX_PORT_STRIDE = 256

OntTarget = tuple[int, int, int]
WalkEntry = tuple[int, int, int, float]


@dataclass
class IngestResult:
    """Summary for a Zabbix ONT signal ingest pass."""

    olts_processed: int = 0
    onts_updated: int = 0
    errors: list[str] = field(default_factory=list)


def _get_client() -> ZabbixClient:
    return ZabbixClient.from_env()


def _decode_huawei_ifindex(encoded: int) -> tuple[int, int] | None:
    if encoded < _HUAWEI_IFINDEX_BASE:
        return None
    offset = encoded - _HUAWEI_IFINDEX_BASE
    snmp_slot = offset // _HUAWEI_IFINDEX_SLOT_STRIDE
    port = (offset % _HUAWEI_IFINDEX_SLOT_STRIDE) // _HUAWEI_IFINDEX_PORT_STRIDE
    return snmp_slot, port


def _parse_external_id(value: str | None) -> OntTarget | None:
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


def _parse_ont_target(ont: OntUnit) -> OntTarget | None:
    parsed = _parse_external_id(ont.external_id)
    if parsed is not None:
        return parsed

    external_id = str(ont.external_id or "").strip()
    if not external_id.isdigit():
        return None

    board = str(ont.board or "").strip()
    port = str(ont.port or "").strip()
    if not port.isdigit():
        return None

    board_parts = [part for part in board.split("/") if part != ""]
    if not board_parts or not board_parts[-1].isdigit():
        return None

    return int(board_parts[-1]), int(port), int(external_id)


def _parse_walk_entries(walk_data: str) -> list[WalkEntry]:
    entries: list[WalkEntry] = []
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


def _is_olt_rx_item(item_key: str) -> bool:
    key = item_key.lower()
    if "onu.rx" in key or "ont.onu.rx" in key:
        return False
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


def _parse_status_code(value: float) -> tuple[OnuOnlineStatus, OnuOfflineReason | None]:
    code = int(value)
    if code == 1:
        return OnuOnlineStatus.online, None
    if code == 3:
        return OnuOnlineStatus.offline, OnuOfflineReason.power_fail
    if code == 4:
        return OnuOnlineStatus.offline, OnuOfflineReason.los
    if code == 5:
        return OnuOnlineStatus.offline, OnuOfflineReason.dying_gasp
    return OnuOnlineStatus.offline, OnuOfflineReason.unknown


def _detect_slot_offset(
    entries: list[WalkEntry],
    targets: set[OntTarget],
) -> int | None:
    votes: dict[int, int] = {}
    for snmp_slot, port, ont_index, _value in entries:
        for target_slot, target_port, target_ont_index in targets:
            if port == target_port and ont_index == target_ont_index:
                offset = snmp_slot - target_slot
                votes[offset] = votes.get(offset, 0) + 1
    if not votes:
        return None
    return max(votes.items(), key=lambda item: item[1])[0]


def _target_from_entry(entry: WalkEntry, slot_offset: int) -> OntTarget:
    snmp_slot, port, ont_index, _value = entry
    return snmp_slot - slot_offset, port, ont_index


def _active_onts_for_olt(db: Session, olt: OLTDevice) -> list[OntUnit]:
    return list(
        db.scalars(
            select(OntUnit)
            .where(OntUnit.olt_device_id == olt.id)
            .where(OntUnit.is_active.is_(True))
        )
    )


def ingest_olt_signal_data(
    db: Session,
    olt: OLTDevice,
    client: ZabbixClient | None = None,
) -> int:
    """Fetch Zabbix walk data for one OLT and persist matching ONT records."""
    if client is None:
        client = _get_client()

    if not olt.zabbix_host_id:
        logger.debug("olt_no_zabbix_host", extra={"olt_id": str(olt.id)})
        return 0

    try:
        items = client.get_items(
            host_ids=[olt.zabbix_host_id],
            metric="walk",
            limit=100,
        )
    except ZabbixClientError:
        logger.exception("zabbix_items_fetch_failed", extra={"olt_id": str(olt.id)})
        raise

    if not items:
        logger.debug("olt_no_items", extra={"olt_id": str(olt.id)})
        return 0

    onts = _active_onts_for_olt(db, olt)
    target_by_ont_id = {
        str(ont.id): parsed
        for ont in onts
        for parsed in [_parse_ont_target(ont)]
        if parsed is not None
    }
    if not target_by_ont_id:
        logger.info(
            "olt_signal_ingest_no_mappable_onts",
            extra={"olt_id": str(olt.id), "onts": len(onts)},
        )
        return 0

    parsed_items: list[tuple[dict, list[WalkEntry]]] = []
    all_entries: list[WalkEntry] = []
    for item in items:
        entries = _parse_walk_entries(str(item.get("lastvalue") or ""))
        if not entries:
            continue
        item_key = str(item.get("key_") or "")
        if not (
            _is_olt_rx_item(item_key)
            or _is_onu_rx_item(item_key)
            or _is_status_item(item_key)
        ):
            continue
        parsed_items.append((item, entries))
        all_entries.extend(entries)

    if not parsed_items:
        logger.debug("olt_no_ont_data", extra={"olt_id": str(olt.id)})
        return 0

    slot_offset = _detect_slot_offset(all_entries, set(target_by_ont_id.values()))
    if slot_offset is None:
        logger.warning(
            "olt_signal_ingest_no_target_matches",
            extra={"olt_id": str(olt.id), "onts": len(target_by_ont_id)},
        )
        return 0

    ont_by_target = {target: ont for ont in onts if (target := _parse_ont_target(ont))}
    ont_data: dict[OntTarget, dict[str, float | bool]] = {}
    for item, entries in parsed_items:
        item_key = str(item.get("key_") or "")
        is_olt_rx = _is_olt_rx_item(item_key)
        is_onu_rx = _is_onu_rx_item(item_key)
        is_status = _is_status_item(item_key)

        for entry in entries:
            target = _target_from_entry(entry, slot_offset)
            if target not in ont_by_target:
                continue
            current = ont_data.setdefault(target, {})
            raw_value = entry[3]
            if is_olt_rx:
                current["_saw_olt_rx"] = True
                dbm = _normalize_signal(raw_value)
                if dbm is not None:
                    current["olt_rx"] = dbm
            elif is_onu_rx:
                current["_saw_onu_rx"] = True
                dbm = _normalize_signal(raw_value)
                if dbm is not None:
                    current["onu_rx"] = dbm
            elif is_status:
                current["status"] = raw_value

    if not ont_data:
        logger.debug("olt_no_matching_ont_data", extra={"olt_id": str(olt.id)})
        return 0

    now = datetime.now(UTC)
    updated_count = 0
    offline_count = 0

    for target, metrics in ont_data.items():
        ont = ont_by_target.get(target)
        if ont is None:
            continue

        polled_status: OnuOnlineStatus | None = None
        offline_reason: OnuOfflineReason | None = None
        if "status" in metrics:
            polled_status, offline_reason = _parse_status_code(float(metrics["status"]))
            apply_olt_status_observation(
                ont,
                polled_status,
                offline_reason,
                now=now,
            )
            if polled_status == OnuOnlineStatus.offline:
                offline_count += 1

        if metrics.get("_saw_olt_rx") and "olt_rx" not in metrics:
            ont.olt_rx_signal_dbm = None
        if "olt_rx" in metrics:
            ont.olt_rx_signal_dbm = float(metrics["olt_rx"])
        if "onu_rx" in metrics:
            ont.onu_rx_signal_dbm = float(metrics["onu_rx"])

        has_signal = any(
            key in metrics for key in ("olt_rx", "onu_rx", "_saw_olt_rx", "_saw_onu_rx")
        )
        if polled_status is not None or has_signal:
            ont.signal_updated_at = now
            ont.last_sync_source = "zabbix_data_ingest"
            ont.last_sync_at = now
            updated_count += 1

    db.flush()
    logger.info(
        "olt_signal_ingest_complete",
        extra={
            "event": "olt_signal_ingest_complete",
            "olt_id": str(olt.id),
            "olt_name": olt.name,
            "onts_updated": updated_count,
            "onts_offline": offline_count,
            "items_processed": len(ont_data),
        },
    )
    return updated_count


def ingest_all_olt_signals(db: Session) -> IngestResult:
    """Ingest persisted ONT signal data for every active Zabbix-linked OLT."""
    result = IngestResult()
    client = _get_client()
    olts = list(
        db.scalars(
            select(OLTDevice).where(
                OLTDevice.is_active.is_(True),
                OLTDevice.zabbix_host_id.is_not(None),
            )
        )
    )

    for olt in olts:
        try:
            updated = ingest_olt_signal_data(db, olt, client=client)
            db.commit()
            result.olts_processed += 1
            result.onts_updated += updated
        except Exception as exc:
            db.rollback()
            result.errors.append(f"{olt.name}: {exc}")
            logger.exception("olt_ingest_exception", extra={"olt_id": str(olt.id)})

    logger.info(
        "signal_ingest_complete",
        extra={
            "event": "signal_ingest_complete",
            "olts_processed": result.olts_processed,
            "onts_updated": result.onts_updated,
            "errors": len(result.errors),
        },
    )
    return result
