"""Topology reconcile: bring Zabbix structure into sub's existing tables.

Phase 1 covers the *matcher* (this module's :func:`match_host`) and the
group/host reconcile. The matcher links a Zabbix host to the provisioning
device (``OLTDevice`` / ``NasDevice``) it represents so that
``resolve_customer_path`` can walk customer -> device -> network_device ->
pop_site, and so unmatched hosts surface in the topology-gaps report instead of
being silently dropped.
"""

from __future__ import annotations

import re
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice
from app.models.network import OLTDevice
from app.models.network_monitoring import DeviceRole, NetworkDevice, PopSite

# Marks rows this reconcile owns (so pruning only touches Zabbix-linked rows).
SOURCE = "zabbix_reconcile"
# Arbitrary constant key for the Postgres advisory lock (single-flight guard).
_ADVISORY_LOCK_KEY = 0x70_70_07  # "topo"
_BTS_SUFFIX = re.compile(r"\s*BTS\s*$", re.IGNORECASE)

# Match outcome for the third element of match_host's return tuple.
MATCHED = "matched"
UNMATCHED = "unmatched"
AMBIGUOUS = "ambiguous"

MatchResult = tuple[str | None, uuid.UUID | None, str]


def _group_names(zhost: dict) -> list[str]:
    return [str(g.get("name", "")) for g in zhost.get("groups", [])]


def _host_ips(zhost: dict) -> list[str]:
    ips: list[str] = []
    for iface in zhost.get("interfaces", []):
        ip = iface.get("ip")
        if ip and ip not in ips:
            ips.append(ip)
    return ips


def _host_label(zhost: dict) -> str:
    return str(zhost.get("host") or zhost.get("name") or "").strip()


def infer_kind(zhost: dict) -> str | None:
    """Infer which provisioning device kind a Zabbix host represents.

    Only an explicit NAS signal narrows the kind: a host named ``NAS: X`` or in
    the ``DotMac/Network/NAS`` group is a NAS. Everything else returns ``None``
    (try both kinds). Crucially, ``*BTS*`` membership is NOT treated as "OLT":
    a BTS group names the *site/pop_site*, and most access devices at a BTS are
    NAS routers, not OLTs. Genuine OLTs still match first by ``zabbix_host_id``.
    """
    label = _host_label(zhost).upper()
    names = [n.upper() for n in _group_names(zhost)]
    if label.startswith("NAS:") or any("NAS" in n for n in names):
        return "nas"
    return None


def _olt_ids_by(session: Session, *filters) -> list[uuid.UUID]:
    q = session.query(OLTDevice.id)
    for f in filters:
        q = q.filter(f)
    return [row[0] for row in q.all()]


def _nas_ids_by(session: Session, *filters) -> list[uuid.UUID]:
    q = session.query(NasDevice.id)
    for f in filters:
        q = q.filter(f)
    return [row[0] for row in q.all()]


def match_host(session: Session, zhost: dict) -> MatchResult:
    """Match a Zabbix host dict to a provisioning device.

    Returns ``(device_type, device_id, status)`` where ``device_type`` is
    ``'olt'``/``'nas'``/``None`` and ``status`` is one of ``matched`` /
    ``unmatched`` / ``ambiguous``.

    Priority: (1) OLTDevice by exact ``zabbix_host_id``; (2) by unique
    management IP (``OLTDevice.mgmt_ip`` / ``NasDevice.management_ip`` — never
    ``nas_ip``/``ip_address``), scoped to the inferred kind; (3) by name, only
    when IP yields no candidate. >1 candidate at any tier -> ``ambiguous``
    (never pick first).
    """
    hostid = str(zhost.get("hostid") or "").strip()

    # (1) Exact Zabbix host id — rename- and re-IP-proof.
    if hostid:
        olt_ids = _olt_ids_by(session, OLTDevice.zabbix_host_id == hostid)
        if len(olt_ids) == 1:
            return ("olt", olt_ids[0], MATCHED)
        if len(olt_ids) > 1:
            return (None, None, AMBIGUOUS)

    kind = infer_kind(zhost)

    # (2) Management IP, scoped to the inferred kind.
    candidates: list[tuple[str, uuid.UUID]] = []
    for ip in _host_ips(zhost):
        if kind in (None, "olt"):
            candidates += [("olt", i) for i in _olt_ids_by(session, OLTDevice.mgmt_ip == ip)]
        if kind in (None, "nas"):
            candidates += [
                ("nas", i) for i in _nas_ids_by(session, NasDevice.management_ip == ip)
            ]
    candidates = list(dict.fromkeys(candidates))
    if len(candidates) == 1:
        t, i = candidates[0]
        return (t, i, MATCHED)
    if len(candidates) > 1:
        return (None, None, AMBIGUOUS)

    # (3) Name fallback — only reached when IP gave nothing.
    label = _host_label(zhost)
    if label:
        nas_label = label.split(":", 1)[1].strip() if ":" in label else label
        name_candidates: list[tuple[str, uuid.UUID]] = []
        if kind in (None, "olt"):
            name_candidates += [
                ("olt", i)
                for i in _olt_ids_by(
                    session, (OLTDevice.hostname == label) | (OLTDevice.name == label)
                )
            ]
        if kind in (None, "nas"):
            name_candidates += [
                ("nas", i)
                for i in _nas_ids_by(
                    session, (NasDevice.name == label) | (NasDevice.name == nas_label)
                )
            ]
        name_candidates = list(dict.fromkeys(name_candidates))
        if len(name_candidates) == 1:
            t, i = name_candidates[0]
            return (t, i, MATCHED)
        if len(name_candidates) > 1:
            return (None, None, AMBIGUOUS)

    return (None, None, UNMATCHED)


# ---------------------------------------------------------------------------
# Reconcile: Zabbix groups -> pop_sites, hosts -> network_devices
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _infer_role(group_names: list[str]) -> DeviceRole:
    up = [g.upper() for g in group_names]
    if any("DATA CENTER" in g or "CORE" in g for g in up):
        return DeviceRole.core
    if any("NAS" in g for g in up):
        return DeviceRole.edge
    if any("BTS" in g for g in up):
        return DeviceRole.access
    return DeviceRole.edge


def _match_pop_site_candidates(session: Session, group_name: str) -> list[PopSite]:
    base = group_name.strip()
    base_no_bts = _BTS_SUFFIX.sub("", base).strip()
    names = {base.lower(), base_no_bts.lower()}
    return (
        session.query(PopSite)
        .filter(
            or_(
                PopSite.name.in_(list(names)),
                PopSite.code.in_(list(names)),
                # case-insensitive fallbacks
                PopSite.name.ilike(base),
                PopSite.name.ilike(base_no_bts),
            )
        )
        .all()
    )


def _find_network_device(
    session: Session, hostid: str, ips: list[str], label: str
) -> tuple[NetworkDevice | None, str]:
    """Find an existing row to merge into; never blind-create."""
    nd = (
        session.query(NetworkDevice)
        .filter(NetworkDevice.zabbix_hostid == hostid)
        .one_or_none()
    )
    if nd is not None:
        return nd, "linked"
    for ip in ips:
        nd = session.query(NetworkDevice).filter(NetworkDevice.mgmt_ip == ip).first()
        if nd is not None:
            return nd, "merged"
    if label:
        nd = (
            session.query(NetworkDevice)
            .filter(or_(NetworkDevice.name == label, NetworkDevice.hostname == label))
            .first()
        )
        if nd is not None:
            return nd, "merged"
    return None, "create"


def _single_flight(session: Session):
    """Postgres advisory lock so scheduled + on-demand runs don't overlap."""
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return nullcontext()
    locked = session.execute(
        text("SELECT pg_try_advisory_lock(:k)"), {"k": _ADVISORY_LOCK_KEY}
    ).scalar()
    if not locked:
        raise RuntimeError("topology reconcile already running")

    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            session.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": _ADVISORY_LOCK_KEY}
            )

    return _Lock()


def _blank_result(dry_run: bool) -> dict:
    return {
        "dry_run": dry_run,
        "pop_sites": {"matched": 0, "backfilled": 0, "created": 0, "ambiguous": 0},
        "network_devices": {
            "linked": 0,
            "merged": 0,
            "created": 0,
            "pruned": 0,
            "duplicate_host": 0,
            "device_matched": 0,
            "unmatched": 0,
            "ambiguous": 0,
        },
    }


# Order so device/BTS hosts claim a shared-IP node before their NAS-monitoring
# sibling: a single network_device row (mgmt_ip is unique) represents the
# physical box, and the OLT/device view is the one customer paths walk to.
def _host_priority(zhost: dict) -> int:
    kind = infer_kind(zhost)
    return {"olt": 0, None: 1, "nas": 2}.get(kind, 1)


def reconcile(session: Session, client, dry_run: bool = False) -> dict:
    """Reconcile Zabbix structure onto pop_sites + network_devices.

    Idempotent: upsert by stable Zabbix ids, match-merge into pre-existing rows
    (never blind-insert duplicates), soft-prune vanished rows. ``dry_run=True``
    computes and returns the same summary WITHOUT writing — for a human review
    before the first live run against the populated tables.
    """
    result = _blank_result(dry_run)
    lock = nullcontext() if dry_run else _single_flight(session)
    with lock:
        # --- Groups -> pop_sites (match-merge by zabbix_group_id / name) ---
        groups = client.get_host_groups()
        bts_groups = [g for g in groups if "BTS" in str(g.get("name", "")).upper()]
        groupid_to_popsite: dict[str, PopSite] = {}
        for g in bts_groups:
            gid, gname = str(g.get("groupid")), str(g.get("name"))
            existing = (
                session.query(PopSite)
                .filter(PopSite.zabbix_group_id == gid)
                .one_or_none()
            )
            if existing is not None:
                groupid_to_popsite[gid] = existing
                result["pop_sites"]["matched"] += 1
                continue
            cands = _match_pop_site_candidates(session, gname)
            if len(cands) == 1:
                ps = cands[0]
                if not dry_run:
                    ps.zabbix_group_id = gid
                groupid_to_popsite[gid] = ps
                result["pop_sites"]["backfilled"] += 1
            elif len(cands) == 0:
                result["pop_sites"]["created"] += 1
                if not dry_run:
                    ps = PopSite(name=gname, zabbix_group_id=gid, is_active=True)
                    session.add(ps)
                    session.flush()
                    groupid_to_popsite[gid] = ps
            else:
                result["pop_sites"]["ambiguous"] += 1

        # --- Hosts -> network_devices (match-merge by hostid / IP / name) ---
        hosts = sorted(client.get_hosts(), key=_host_priority)
        current_hostids: set[str] = {
            str(h.get("hostid")).strip() for h in hosts if h.get("hostid")
        }
        claimed_node_ids: set = set()  # node identities claimed this run
        for zhost in hosts:
            hostid = str(zhost.get("hostid") or "").strip()
            if not hostid:
                continue
            ips = _host_ips(zhost)
            label = _host_label(zhost)
            gnames = _group_names(zhost)

            nd, action = _find_network_device(session, hostid, ips, label)

            # Same-IP sibling: another Zabbix host already claimed this node this
            # run (device hosts sort ahead of their NAS monitoring sibling) ->
            # don't stomp its zabbix_hostid/match, just touch it.
            if nd is not None and nd.id in claimed_node_ids:
                result["network_devices"]["duplicate_host"] += 1
                if not dry_run:
                    nd.last_synced_at = _now()
                    session.flush()
                continue
            if nd is not None:
                claimed_node_ids.add(nd.id)

            if action == "create":
                result["network_devices"]["created"] += 1
            else:
                result["network_devices"][action] += 1

            mt, mid, mstatus = match_host(session, zhost)
            result["network_devices"][
                {MATCHED: "device_matched", UNMATCHED: "unmatched", AMBIGUOUS: "ambiguous"}[
                    mstatus
                ]
            ] += 1

            if dry_run:
                continue

            if nd is None:
                nd = NetworkDevice(
                    name=label or f"zabbix-{hostid}",
                    hostname=label or None,
                    mgmt_ip=ips[0] if ips else None,
                    role=_infer_role(gnames),
                    role_source="inferred",
                    is_active=True,
                )
                session.add(nd)
            else:
                if not nd.mgmt_ip and ips:
                    nd.mgmt_ip = ips[0]
                # Never stomp a manually-set role.
                if nd.role_source in (None, "inferred"):
                    nd.role = _infer_role(gnames)
                    nd.role_source = "inferred"
                nd.is_active = True
            nd.zabbix_hostid = hostid
            nd.source = SOURCE
            nd.last_synced_at = _now()
            nd.matched_device_type = mt
            nd.matched_device_id = mid
            for grp in zhost.get("groups", []):
                if str(grp.get("groupid")) in groupid_to_popsite:
                    nd.pop_site_id = groupid_to_popsite[str(grp.get("groupid"))].id
                    break
            session.flush()
            claimed_node_ids.add(nd.id)  # a newly created node is now claimed

        # --- Soft-prune: linked rows whose Zabbix host vanished ---
        stale_q = session.query(NetworkDevice).filter(
            NetworkDevice.zabbix_hostid.isnot(None),
            NetworkDevice.is_active.is_(True),
        )
        if current_hostids:
            stale_q = stale_q.filter(
                NetworkDevice.zabbix_hostid.notin_(list(current_hostids))
            )
        if dry_run:
            result["network_devices"]["pruned"] = stale_q.count()
        else:
            stale = stale_q.all()
            for nd in stale:
                nd.is_active = False
            result["network_devices"]["pruned"] = len(stale)
            session.flush()

    return result
