"""Topology reconcile: bring Zabbix structure into sub's existing tables.

Phase 1 covers the *matcher* (this module's :func:`match_host`) and the
group/host reconcile. The matcher links a Zabbix host to the provisioning
device (``OLTDevice`` / ``NasDevice``) it represents so that
``resolve_customer_path`` can walk customer -> device -> network_device ->
pop_site, and so unmatched hosts surface in the topology-gaps report instead of
being silently dropped.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models.catalog import NasDevice
from app.models.network import OLTDevice

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

    Used to disambiguate the ~2-hosts-per-IP case: a device host lives in a
    ``*BTS*`` group, while its NAS counterpart is named ``NAS: X`` / sits in the
    ``DotMac/Network/NAS`` group. Returns ``'olt'``, ``'nas'`` or ``None``
    (unknown -> try both kinds).
    """
    label = _host_label(zhost).upper()
    names = [n.upper() for n in _group_names(zhost)]
    if label.startswith("NAS:") or any("NAS" in n for n in names):
        return "nas"
    if any("BTS" in n for n in names):
        return "olt"
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
