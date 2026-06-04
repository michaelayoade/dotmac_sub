"""Read ONT status and optical signal data directly from Zabbix."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.metrics import (
    observe_cache_refresh,
    record_cache_fallback,
    record_cache_lookup,
)
from app.services import app_cache
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
_HUAWEI_IFINDEX_SLOT_STRIDE = 8192
_HUAWEI_IFINDEX_PORT_STRIDE = 256
_DEFAULT_WALK_CACHE_TTL_SECONDS = 30
_DEFAULT_ONT_SNAPSHOT_CACHE_TTL_SECONDS = 180


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
    snmp_slot = offset // _HUAWEI_IFINDEX_SLOT_STRIDE
    port = (offset % _HUAWEI_IFINDEX_SLOT_STRIDE) // _HUAWEI_IFINDEX_PORT_STRIDE
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


def _walk_cache_ttl_seconds() -> int:
    raw = os.getenv("ZABBIX_ONT_WALK_CACHE_TTL_SECONDS")
    if raw is None:
        return _DEFAULT_WALK_CACHE_TTL_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_WALK_CACHE_TTL_SECONDS


def _ont_snapshot_cache_ttl_seconds() -> int:
    raw = os.getenv("ZABBIX_ONT_SNAPSHOT_CACHE_TTL_SECONDS")
    if raw is None:
        return _DEFAULT_ONT_SNAPSHOT_CACHE_TTL_SECONDS
    try:
        return max(60, int(raw))
    except ValueError:
        return _DEFAULT_ONT_SNAPSHOT_CACHE_TTL_SECONDS


def _snapshot_cache_key_for_olt(olt_id: object) -> str:
    return app_cache.cache_key("ont-zabbix-snapshot", olt_id)


def _serialize_snapshot(
    snapshot: dict[str, OntSignalData],
) -> dict[str, dict[str, object]]:
    payload: dict[str, dict[str, object]] = {}
    for ont_id, signal in snapshot.items():
        payload[ont_id] = {
            "online": signal.online,
            "olt_rx_dbm": signal.olt_rx_dbm,
            "onu_rx_dbm": signal.onu_rx_dbm,
            "updated_at": signal.updated_at.isoformat() if signal.updated_at else None,
            "error": signal.error,
        }
    return payload


def _deserialize_snapshot(
    payload: dict[str, dict[str, object]],
) -> dict[str, OntSignalData]:
    snapshot: dict[str, OntSignalData] = {}
    for ont_id, raw in payload.items():
        updated_at_raw = raw.get("updated_at")
        updated_at = None
        if isinstance(updated_at_raw, str) and updated_at_raw:
            try:
                updated_at = datetime.fromisoformat(updated_at_raw)
            except ValueError:
                updated_at = None
        olt_rx_raw = raw.get("olt_rx_dbm")
        olt_rx_dbm = float(olt_rx_raw) if isinstance(olt_rx_raw, (float, int)) else None
        onu_rx_raw = raw.get("onu_rx_dbm")
        onu_rx_dbm = float(onu_rx_raw) if isinstance(onu_rx_raw, (float, int)) else None
        snapshot[ont_id] = OntSignalData(
            online=bool(raw.get("online")),
            olt_rx_dbm=olt_rx_dbm,
            onu_rx_dbm=onu_rx_dbm,
            updated_at=updated_at,
            error=str(raw.get("error")) if raw.get("error") is not None else None,
        )
    return snapshot


def get_cached_olt_snapshot(olt_id: object) -> dict[str, OntSignalData] | None:
    payload = app_cache.get_json(_snapshot_cache_key_for_olt(olt_id))
    if not isinstance(payload, dict):
        return None
    try:
        normalized = {
            str(ont_id): value
            for ont_id, value in payload.items()
            if isinstance(value, dict)
        }
        return _deserialize_snapshot(normalized)
    except Exception:
        return None


def set_cached_olt_snapshot(olt_id: object, snapshot: dict[str, OntSignalData]) -> bool:
    return app_cache.set_json(
        _snapshot_cache_key_for_olt(olt_id),
        _serialize_snapshot(snapshot),
        _ont_snapshot_cache_ttl_seconds(),
    )


def _cache_key_for_olt_walk(host_id: object) -> str:
    return f"zabbix:olt-walk:{host_id}"


def _get_cached_walk_items(host_id: object) -> list[dict] | None:
    try:
        from app.services.redis_client import safe_get

        cached = safe_get(_cache_key_for_olt_walk(host_id))
    except Exception:
        return None
    if not cached:
        return None
    try:
        payload = json.loads(str(cached))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, list):
        return None
    return [item for item in payload if isinstance(item, dict)]


def _set_cached_walk_items(host_id: object, items: list[dict]) -> None:
    ttl = _walk_cache_ttl_seconds()
    if ttl <= 0:
        return
    try:
        from app.services.redis_client import safe_set

        safe_set(_cache_key_for_olt_walk(host_id), json.dumps(items), ttl=ttl)
    except Exception:
        return


def _get_walk_items_for_olt(olt: OLTDevice, client: ZabbixClient | None) -> list[dict]:
    host_id = getattr(olt, "zabbix_host_id", None)
    if client is not None:
        # Injected clients are used by tests and one-shot callers; avoid hiding
        # direct client behavior behind Redis state in those paths.
        return client.get_items(host_ids=[host_id], metric="walk", limit=100)  # type: ignore[list-item]
    cached = _get_cached_walk_items(host_id)
    if cached is not None:
        return cached
    zbx = ZabbixClient.from_env()
    items = zbx.get_items(host_ids=[host_id], metric="walk", limit=100)  # type: ignore[list-item]
    _set_cached_walk_items(host_id, items)
    return items


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


def _normalize_signal(raw_value: float, *, is_onu_rx: bool = False) -> float | None:
    if is_onu_rx and raw_value > 1000:
        dbm = (raw_value - 10000) / 100.0
    else:
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
        items = _get_walk_items_for_olt(olt, client)
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
        ont_id: {"online": False, "status_seen": False, "error": None}
        for ont_id in targets
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
                if previous is None or timestamp > previous:  # type: ignore[operator]
                    current["updated_at"] = timestamp
            if is_status:
                current["online"] = _parse_status_code(raw_value)
                current["status_seen"] = True
            if is_rx:
                dbm = _normalize_signal(raw_value, is_onu_rx=is_onu_rx)
                if dbm is None:
                    continue
                if is_onu_rx:
                    current["onu_rx_dbm"] = dbm
                else:
                    current["olt_rx_dbm"] = dbm

    for ont_id, current in values.items():
        error = current.get("error")
        if not current.get("status_seen"):
            error = "ONT status not found in Zabbix"
        result[ont_id] = OntSignalData(
            online=bool(current.get("online")),
            olt_rx_dbm=current.get("olt_rx_dbm"),  # type: ignore[arg-type]
            onu_rx_dbm=current.get("onu_rx_dbm"),  # type: ignore[arg-type]
            updated_at=current.get("updated_at"),  # type: ignore[arg-type]
            error=error,  # type: ignore[arg-type]
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
    *,
    cached_only: bool = False,
) -> dict[str, OntSignalData]:
    """Return Zabbix snapshots for ONTs grouped by their OLT.

    With ``cached_only=True``, skip live Zabbix walks on cache miss and return
    only cached entries. Callers (e.g. inventory list rendering) use this to
    avoid blocking the page on a fan-out of per-OLT Zabbix calls.
    """
    from app.models.network import OLTDevice

    if cached_only:
        result: dict[str, OntSignalData] = {}
    else:
        result = {
            str(getattr(ont, "id", "")): _offline("OLT not linked to ONT")
            for ont in onts
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
        cached = get_cached_olt_snapshot(olt_id)
        if cached is not None:
            record_cache_lookup("ont_zabbix_snapshot", "hit")
            missing_onts: list[OntUnit] = []
            for ont in olt_onts:
                ont_key = str(getattr(ont, "id", ""))
                if ont_key in cached:
                    result[ont_key] = cached[ont_key]
                else:
                    missing_onts.append(ont)
            if not missing_onts:
                continue
            if cached_only:
                continue
            record_cache_fallback("ont_zabbix_snapshot", "live_fetch")
            olt = db.get(OLTDevice, olt_id)
            if olt is None:
                continue
            snapshot = get_olt_ont_snapshot_from_zabbix(olt, missing_onts)
            merged_snapshot = dict(cached)
            merged_snapshot.update(snapshot)
            set_cached_olt_snapshot(olt_id, merged_snapshot)
            result.update(snapshot)
            continue

        record_cache_lookup("ont_zabbix_snapshot", "miss")
        if cached_only:
            continue
        record_cache_fallback("ont_zabbix_snapshot", "live_fetch")
        olt = db.get(OLTDevice, olt_id)
        if olt is None:
            continue
        snapshot = get_olt_ont_snapshot_from_zabbix(olt, olt_onts)
        set_cached_olt_snapshot(olt_id, snapshot)
        result.update(snapshot)
    return result


def refresh_all_olt_snapshots_cache(db: Session) -> dict[str, int]:
    from app.models.network import OLTDevice, OntUnit

    started_at = datetime.now(UTC)
    result = {"olts_cached": 0, "onts_cached": 0, "errors": 0}
    olts = (
        db.query(OLTDevice)
        .filter(OLTDevice.is_active.is_(True))
        .filter(OLTDevice.zabbix_host_id.isnot(None))
        .all()
    )
    try:
        for olt in olts:
            onts = (
                db.query(OntUnit)
                .filter(OntUnit.is_active.is_(True))
                .filter(OntUnit.olt_device_id == olt.id)
                .all()
            )
            if not onts:
                continue
            try:
                snapshot = get_olt_ont_snapshot_from_zabbix(olt, onts)
                if set_cached_olt_snapshot(olt.id, snapshot):
                    result["olts_cached"] += 1
                    result["onts_cached"] += len(snapshot)
            except Exception:
                logger.debug(
                    "ont_snapshot_cache_refresh_failed",
                    extra={"olt_id": str(getattr(olt, "id", ""))},
                    exc_info=True,
                )
                result["errors"] += 1
        observe_cache_refresh(
            "ont_zabbix_snapshot",
            "success" if result["errors"] == 0 else "partial",
            (datetime.now(UTC) - started_at).total_seconds(),
        )
        return result
    except Exception:
        observe_cache_refresh(
            "ont_zabbix_snapshot",
            "failure",
            (datetime.now(UTC) - started_at).total_seconds(),
        )
        raise


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
        # ``olt.zabbix_host_id`` is ``str | None`` per the schema. This
        # branch is gated above on the value being set; coerce to ``str``
        # so the list literal type-checks without an inline suppression.
        zabbix_host_id = str(olt.zabbix_host_id or "")
        items = client.get_items(
            host_ids=[zabbix_host_id],
            metric="ont.count",
            limit=10,
        )
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
    return result  # type: ignore[return-value]
