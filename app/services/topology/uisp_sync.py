"""UISP topology sync: import the customer-device relationship layer.

Pulls the UISP inventory (devices + sites, read-only) and reconciles the
wireless/UFiber customer layer into sub's own tables:

  - wireless customer radios (airMax stations, airCube/blackBox) -> cpe_devices,
    keyed by ``uisp_device_id``, with the CPE -> AP edge stored as
    ``parent_network_device_id`` (FK to the network_devices row the Zabbix
    reconcile owns) so customer paths can walk radio -> AP -> basestation;
  - UF-OLTs -> olt_devices and UFiber ONUs -> ont_units (parent resolved via
    the ONU's ``attributes.parentId``), both keyed by ``uisp_device_id``.

Monitoring/state stays with the Zabbix layer (the co-located UISP->Zabbix
importer feeds it); this sync only owns *relationships* and identity fill-in.

Idioms follow zabbix_reconcile/lldp_poller: idempotent upsert by stable
external id, match-don't-create against pre-existing rows, never overwrite a
non-NULL human-set field, per-item failure isolation, soft-prune limited to
the columns this sync owns.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import CPEDevice, DeviceType, OLTDevice, OntUnit
from app.models.network_monitoring import NetworkDevice
from app.services.topology.lldp_poller import _norm as _norm_label
from app.services.topology.lldp_poller import build_device_index

logger = logging.getLogger(__name__)

# Marks provenance on rows/edges this sync owns.
SOURCE = "uisp_sync"
# The UISP "Archive" site: decommissioned gear parked there is never imported.
ARCHIVE_SITE_ID = "d857d634-db38-45ff-81a1-4594410ded45"
# Arbitrary constant key for the Postgres advisory lock (single-flight guard,
# acquired via db_session_adapter.advisory_lock in the Celery task wrapper).
ADVISORY_LOCK_KEY = 0x75_69_53  # "uiS"

# Device types that are customer radios even when the role field is absent.
_STATION_TYPES = {"airCube", "blackBox"}
# Factory-default addresses several stations report; never unique, never keyed.
_SHARED_DEFAULT_IPS = {"192.168.1.1", "192.168.1.20"}

_MAC_JUNK = re.compile(r"[^0-9a-f]")


def _now() -> datetime:
    return datetime.now(UTC)


def _norm_mac(mac: str | None) -> str | None:
    """Normalize a MAC to bare lowercase hex; None when unusable."""
    if not mac:
        return None
    normalized = _MAC_JUNK.sub("", str(mac).lower())
    return normalized if len(normalized) == 12 else None


def _ident(device: dict) -> dict:
    ident = device.get("identification")
    return ident if isinstance(ident, dict) else {}


def _attributes(device: dict) -> dict:
    attrs = device.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _device_id(device: dict) -> str:
    return str(_ident(device).get("id") or "").strip()


def _device_name(device: dict) -> str:
    return str(_ident(device).get("name") or "").strip()


def _device_role(device: dict) -> str:
    return str(_ident(device).get("role") or "").strip().lower()


def _device_type(device: dict) -> str:
    return str(_ident(device).get("type") or "").strip()


def _device_status(device: dict) -> str | None:
    overview = device.get("overview")
    if not isinstance(overview, dict):
        return None
    status = str(overview.get("status") or "").strip()
    return status[:20] or None


def _device_site_id(device: dict) -> str | None:
    site = _ident(device).get("site")
    if not isinstance(site, dict):
        return None
    sid = str(site.get("id") or "").strip()
    return sid or None


def _mgmt_ip(device: dict) -> str | None:
    """The device's mgmt IP without its prefix length; None when absent."""
    raw = str(device.get("ipAddress") or "").strip()
    if not raw:
        return None
    return raw.split("/", 1)[0]


def _serial(device: dict) -> str | None:
    ident = _ident(device)
    for key in ("serialNumber", "serial"):
        value = str(ident.get(key) or "").strip()
        if value:
            return value
    return None


def _is_station(device: dict) -> bool:
    return _device_role(device) == "station" or _device_type(device) in _STATION_TYPES


def _is_ap(device: dict) -> bool:
    return _device_role(device) == "ap"


def _is_olt(device: dict) -> bool:
    return _device_type(device) == "olt"


def _is_onu(device: dict) -> bool:
    return _device_type(device) == "onu"


def excluded_site_ids(sites: list[dict]) -> set[str]:
    """The archive site plus every site parented under it (endpoints)."""
    excluded = {ARCHIVE_SITE_ID}
    for site in sites:
        sid = str(site.get("id") or "").strip()
        ident = site.get("identification")
        parent = (ident or {}).get("parent") if isinstance(ident, dict) else None
        parent_id = (
            str(parent.get("id") or "").strip() if isinstance(parent, dict) else ""
        )
        if sid and parent_id == ARCHIVE_SITE_ID:
            excluded.add(sid)
    return excluded


def _unique_ips(devices: list[dict]) -> set[str]:
    """IPs that appear on exactly one UISP device (and are not shared defaults)."""
    counts = Counter(ip for d in devices if (ip := _mgmt_ip(d)))
    return {ip for ip, n in counts.items() if n == 1 and ip not in _SHARED_DEFAULT_IPS}


def _active_subscriber_macs(session: Session) -> dict[str, set]:
    """Normalized MAC -> distinct subscriber ids over ACTIVE subscriptions only.

    The subscriptions table carries historical duplicate rows; scoping to
    status='active' and collapsing to distinct subscribers means a MAC that
    still maps to >1 subscriber is genuinely ambiguous and must stay unlinked.
    """
    index: dict[str, set] = {}
    rows = (
        session.query(Subscription.mac_address, Subscription.subscriber_id)
        .filter(
            Subscription.status == SubscriptionStatus.active,
            Subscription.mac_address.isnot(None),
        )
        .all()
    )
    for mac, subscriber_id in rows:
        normalized = _norm_mac(mac)
        if normalized:
            index.setdefault(normalized, set()).add(subscriber_id)
    return index


def _fill(obj, attr: str, value) -> bool:
    """Fill a NULL field only; never overwrite human-set data. True if set."""
    if value is None or value == "":
        return False
    if getattr(obj, attr) is None:
        setattr(obj, attr, value)
        return True
    return False


# ---------------------------------------------------------------------------
# AP matching (UISP AP -> network_devices node)
# ---------------------------------------------------------------------------


def match_ap_nodes(
    session: Session,
    aps: list[dict],
    unique_ips: set[str],
    stats: Counter,
) -> dict[str, NetworkDevice]:
    """Match UISP APs to network_devices rows (mgmt IP first, then name).

    Stamps ``uisp_device_id`` on matched rows and returns uisp AP id -> node.
    The Zabbix reconcile creates/owns these rows; this sync never creates
    network_devices, it only links them.
    """
    by_name, by_ip = build_device_index(session)
    ap_nodes: dict[str, NetworkDevice] = {}
    for ap in aps:
        uisp_id = _device_id(ap)
        if not uisp_id:
            continue
        node = None
        ip = _mgmt_ip(ap)
        if ip and ip in unique_ips:
            node = by_ip.get(ip)
        if node is None:
            node = by_name.get(_norm_label(_device_name(ap)))
        if node is None:
            stats["aps_unmatched"] += 1
            continue
        if uisp_id in ap_nodes:
            continue
        if node.uisp_device_id != uisp_id:
            # The AP moved to a different node (re-IP/rename): release the id
            # from any other row first so the partial-unique index holds.
            session.query(NetworkDevice).filter(
                NetworkDevice.uisp_device_id == uisp_id,
                NetworkDevice.id != node.id,
            ).update({NetworkDevice.uisp_device_id: None}, synchronize_session=False)
            node.uisp_device_id = uisp_id
        ap_nodes[uisp_id] = node
        stats["aps_matched"] += 1
    return ap_nodes


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def _blank_stats() -> Counter:
    return Counter(
        {
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "edges_set": 0,
            "matched": 0,
            "ambiguous": 0,
            "skipped": 0,
            "failed": 0,
            "pruned": 0,
            "excluded_archive": 0,
            "aps_matched": 0,
            "aps_unmatched": 0,
        }
    )


def _station_parent_ap_id(station: dict) -> str | None:
    ap_device = _attributes(station).get("apDevice")
    if isinstance(ap_device, dict):
        ap_id = str(ap_device.get("id") or "").strip()
        if ap_id:
            return ap_id
    return None


def _ap_side_station_index(
    client, aps: list[dict], ap_nodes: dict[str, NetworkDevice]
) -> tuple[dict[str, str], dict[str, str]]:
    """AP-side association lists: station uisp id / MAC -> AP uisp id.

    Only queried for ACTIVE, node-matched APs, and only used as a fallback for
    stations whose payload lacks ``attributes.apDevice``.
    """
    by_station_id: dict[str, str] = {}
    by_station_mac: dict[str, str] = {}
    for ap in aps:
        ap_id = _device_id(ap)
        if not ap_id or ap_id not in ap_nodes or _device_status(ap) != "active":
            continue
        try:
            entries = client.list_airmax_stations(ap_id)
        except Exception as exc:  # one AP failing must not abort the run
            logger.warning("uisp_ap_station_list_failed ap=%s: %s", ap_id, exc)
            continue
        for entry in entries:
            ident = entry.get("deviceIdentification")
            station_id = (
                str(ident.get("id") or "").strip() if isinstance(ident, dict) else ""
            )
            if station_id:
                by_station_id.setdefault(station_id, ap_id)
            mac = _norm_mac(entry.get("mac"))
            if mac:
                by_station_mac.setdefault(mac, ap_id)
    return by_station_id, by_station_mac


def _upsert_station(
    session: Session,
    station: dict,
    now: datetime,
    stats: Counter,
) -> tuple[CPEDevice, bool]:
    """Upsert one wireless customer radio into cpe_devices by uisp_device_id."""
    uisp_id = _device_id(station)
    cpe = (
        session.query(CPEDevice)
        .filter(CPEDevice.uisp_device_id == uisp_id)
        .one_or_none()
    )
    created = cpe is None
    changed = False
    if cpe is None:
        cpe = CPEDevice(
            uisp_device_id=uisp_id,
            device_type=DeviceType.wireless_radio,
            vendor="ubiquiti",
        )
        session.add(cpe)

    ident = _ident(station)
    changed |= _fill(cpe, "mac_address", ident.get("mac"))
    changed |= _fill(cpe, "model", ident.get("model"))
    changed |= _fill(cpe, "serial_number", _serial(station))
    changed |= _fill(cpe, "vendor", "ubiquiti")
    # Upgrade only the placeholder type; a human-chosen type is never stomped.
    if not created and cpe.device_type in (None, DeviceType.other):
        cpe.device_type = DeviceType.wireless_radio
        changed = True

    status = _device_status(station)
    if status and cpe.last_uisp_status != status:
        cpe.last_uisp_status = status
        changed = True
    cpe.uisp_synced_at = now

    session.flush()
    if created:
        stats["created"] += 1
    elif changed:
        stats["updated"] += 1
    else:
        stats["unchanged"] += 1
    return cpe, created


def _upsert_olt(
    session: Session,
    device: dict,
    unique_ips: set[str],
    stats: Counter,
) -> OLTDevice | None:
    """Upsert a UF-OLT into olt_devices (match-don't-create, then create)."""
    uisp_id = _device_id(device)
    name = _device_name(device)
    ip = _mgmt_ip(device)
    olt = (
        session.query(OLTDevice)
        .filter(OLTDevice.uisp_device_id == uisp_id)
        .one_or_none()
    )
    if olt is None and ip and ip in unique_ips:
        olt = session.query(OLTDevice).filter(OLTDevice.mgmt_ip == ip).one_or_none()
    if olt is None and name:
        candidates = (
            session.query(OLTDevice)
            .filter(or_(OLTDevice.hostname == name, OLTDevice.name == name))
            .all()
        )
        if len(candidates) == 1:
            olt = candidates[0]

    created = olt is None
    changed = False
    if olt is None:
        olt = OLTDevice(name=name or f"uisp-{uisp_id[:8]}", vendor="ubiquiti")
        session.add(olt)

    ident = _ident(device)
    changed |= _fill(olt, "vendor", "ubiquiti")
    changed |= _fill(olt, "model", ident.get("model"))
    changed |= _fill(olt, "serial_number", _serial(device))
    if ip and ip in unique_ips:
        changed |= _fill(olt, "mgmt_ip", ip)
    if olt.uisp_device_id != uisp_id:
        # Release the id from any other row first (a fallback ip/name match
        # can land on a row while another still holds this uisp id) so the
        # partial-unique index can't reject the flush.
        release = session.query(OLTDevice).filter(OLTDevice.uisp_device_id == uisp_id)
        if olt.id is not None:
            release = release.filter(OLTDevice.id != olt.id)
        release.update({OLTDevice.uisp_device_id: None}, synchronize_session=False)
        olt.uisp_device_id = uisp_id
        changed = True

    session.flush()
    if created:
        stats["created"] += 1
    elif changed:
        stats["updated"] += 1
    else:
        stats["unchanged"] += 1
    return olt


def _upsert_onu(
    session: Session,
    device: dict,
    olt_ids_by_uisp: dict[str, UUID],
    seen_onu_keys: set[tuple],
    now: datetime,
    stats: Counter,
) -> OntUnit | None:
    """Upsert a UFiber ONU into ont_units; parent via attributes.parentId."""
    uisp_id = _device_id(device)
    parent_uisp_id = str(_attributes(device).get("parentId") or "").strip()
    olt_id = olt_ids_by_uisp.get(parent_uisp_id)
    if olt_id is None:
        stats["skipped"] += 1
        return None

    ident = _ident(device)
    mac = _norm_mac(ident.get("mac"))
    serial = _serial(device) or (mac.upper() if mac else None) or f"UISP-{uisp_id[:8]}"

    # Guard the (olt_device_id, serial_number) unique key in code: a second
    # UISP device collapsing onto the same key this run is skipped rather than
    # flushed into an IntegrityError that would poison the session.
    key = (olt_id, serial)
    if key in seen_onu_keys:
        stats["skipped"] += 1
        return None
    seen_onu_keys.add(key)

    ont = session.query(OntUnit).filter(OntUnit.uisp_device_id == uisp_id).one_or_none()
    if ont is None:
        ont = (
            session.query(OntUnit)
            .filter(OntUnit.olt_device_id == olt_id, OntUnit.serial_number == serial)
            .one_or_none()
        )

    created = ont is None
    changed = False
    if ont is None:
        # pon_port stays NULL: the UISP list payload has no port granularity.
        ont = OntUnit(serial_number=serial, olt_device_id=olt_id, vendor="ubiquiti")
        session.add(ont)
    elif ont.olt_device_id is None:
        ont.olt_device_id = olt_id
        changed = True

    changed |= _fill(ont, "name", ident.get("name"))
    changed |= _fill(ont, "model", ident.get("model"))
    changed |= _fill(ont, "vendor", "ubiquiti")
    changed |= _fill(ont, "mac_address", ident.get("mac"))
    if ont.uisp_device_id != uisp_id:
        # Release the id from any other row first (the (olt, serial) fallback
        # can land on a row while another still holds this uisp id) so the
        # partial-unique index can't reject the flush.
        release = session.query(OntUnit).filter(OntUnit.uisp_device_id == uisp_id)
        if ont.id is not None:
            release = release.filter(OntUnit.id != ont.id)
        release.update({OntUnit.uisp_device_id: None}, synchronize_session=False)
        ont.uisp_device_id = uisp_id
        changed = True
    ont.last_sync_source = "uisp"
    ont.last_sync_at = now

    session.flush()
    if created:
        stats["created"] += 1
    elif changed:
        stats["updated"] += 1
    else:
        stats["unchanged"] += 1
    return ont


def sync(session: Session, client, now: datetime | None = None) -> dict:
    """Run one UISP topology sync pass; returns the counter summary.

    Read-only against UISP. Idempotent: every row is keyed by its stable
    ``uisp_device_id``; re-runs only bump sync timestamps. Each device is
    upserted inside its own SAVEPOINT (``Session.begin_nested``, the
    zabbix_host_sync idiom): a flush failure rolls back to the savepoint,
    is counted as ``failed`` and never aborts the run or poisons the
    session. Single-flight locking lives in the Celery task wrapper
    (``db_session_adapter.advisory_lock``), not here.
    """
    now = now or _now()
    stats = _blank_stats()
    sites = client.list_sites()
    devices = client.list_devices()
    excluded_sites = excluded_site_ids(sites)

    kept: list[dict] = []
    for device in devices:
        if not _device_id(device):
            continue
        site_id = _device_site_id(device)
        if site_id and site_id in excluded_sites:
            stats["excluded_archive"] += 1
            continue
        kept.append(device)

    unique_ips = _unique_ips(kept)
    aps = [d for d in kept if _is_ap(d)]
    olts = [d for d in kept if _is_olt(d)]
    onus = [d for d in kept if _is_onu(d)]
    stations = [d for d in kept if _is_station(d) and not _is_olt(d) and not _is_onu(d)]

    # --- APs -> network_devices (match-only; stamps uisp_device_id) ---
    ap_nodes = match_ap_nodes(session, aps, unique_ips, stats)
    session.flush()

    # --- Wireless customer radios -> cpe_devices ---
    # PtP backhaul masters report role=station but are infrastructure: a
    # station whose name matches an existing network_devices row must not
    # create a duplicate CPE (match-don't-create).
    infra_names, _infra_ips = build_device_index(session)
    need_ap_fallback = any(
        _station_parent_ap_id(s) is None
        and _norm_label(_device_name(s)) not in infra_names
        for s in stations
    )
    ap_by_station_id: dict[str, str] = {}
    ap_by_station_mac: dict[str, str] = {}
    if need_ap_fallback:
        ap_by_station_id, ap_by_station_mac = _ap_side_station_index(
            client, aps, ap_nodes
        )

    created_cpes: list[CPEDevice] = []
    # Seen = what UISP *reports*, taken from the raw inventory BEFORE the
    # upsert loop: a device whose upsert fails transiently is still present
    # in UISP and must not be soft-pruned to 'vanished'.
    seen_cpe_uisp_ids: set[str] = {_device_id(s) for s in stations}
    for station in stations:
        uisp_id = _device_id(station)
        if _norm_label(_device_name(station)) in infra_names:
            stats["skipped"] += 1
            continue
        try:
            # SAVEPOINT per item (the zabbix_host_sync idiom): a flush failure
            # rolls back only this device, not the surrounding transaction.
            with session.begin_nested():
                cpe, created = _upsert_station(session, station, now, stats)
                parent_ap_id = (
                    _station_parent_ap_id(station)
                    or ap_by_station_id.get(uisp_id)
                    or ap_by_station_mac.get(
                        _norm_mac(_ident(station).get("mac")) or ""
                    )
                )
                node = ap_nodes.get(parent_ap_id) if parent_ap_id else None
                if node is not None and cpe.parent_network_device_id != node.id:
                    cpe.parent_network_device_id = node.id
                    stats["edges_set"] += 1
                session.flush()
        except Exception:
            stats["failed"] += 1
            logger.exception("uisp_sync_station_failed uisp_id=%s", uisp_id)
            continue
        if created:
            created_cpes.append(cpe)

    # --- Subscriber matching: NEWLY CREATED radios only, exact MAC ---
    if created_cpes:
        mac_index = _active_subscriber_macs(session)
        for cpe in created_cpes:
            normalized = _norm_mac(cpe.mac_address)
            subscribers = mac_index.get(normalized or "", set())
            if len(subscribers) == 1:
                cpe.subscriber_id = next(iter(subscribers))
                stats["matched"] += 1
            elif len(subscribers) > 1:
                stats["ambiguous"] += 1
        session.flush()

    # --- UF-OLTs -> olt_devices ---
    olt_ids_by_uisp: dict[str, UUID] = {}
    for device in olts:
        try:
            with session.begin_nested():
                olt = _upsert_olt(session, device, unique_ips, stats)
        except Exception:
            stats["failed"] += 1
            logger.exception("uisp_sync_olt_failed uisp_id=%s", _device_id(device))
            continue
        if olt is not None:
            olt_ids_by_uisp[_device_id(device)] = olt.id

    # --- UFiber ONUs -> ont_units ---
    seen_onu_keys: set[tuple] = set()
    for device in onus:
        try:
            with session.begin_nested():
                _upsert_onu(session, device, olt_ids_by_uisp, seen_onu_keys, now, stats)
        except Exception:
            stats["failed"] += 1
            logger.exception("uisp_sync_onu_failed uisp_id=%s", _device_id(device))

    # --- Soft-prune: radios that vanished from UISP ---
    # Only the columns this sync owns are touched; operator-managed fields
    # (status, subscriber link) are never flipped by a prune.
    stale = (
        session.query(CPEDevice)
        .filter(
            CPEDevice.uisp_device_id.isnot(None),
            or_(
                CPEDevice.last_uisp_status.is_(None),
                CPEDevice.last_uisp_status != "vanished",
            ),
        )
        .all()
    )
    for cpe in stale:
        if cpe.uisp_device_id not in seen_cpe_uisp_ids:
            cpe.last_uisp_status = "vanished"
            cpe.uisp_synced_at = now
            stats["pruned"] += 1
    session.flush()

    result = dict(stats)
    log_extra = {"event": "uisp_topology_sync_complete"}
    log_extra.update({f"uisp_{key}": value for key, value in result.items()})
    logger.info(
        "uisp_topology_sync_complete",
        extra=log_extra,
    )
    return result
