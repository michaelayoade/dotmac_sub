"""Reviewed forwarding declarations and observation-only agreement projection.

Sub declarations are the official downstream-to-upstream path. LLDP, BGP,
routing-table, and RADIUS data remain observations: they can prove agreement or
drift but can never create, change, or retire a declaration. Configuration is
applied by the referenced configuration owner, never by this service.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.forwarding_topology import (
    ForwardingControlObservation,
    ForwardingTopologyDecision,
    ForwardingTopologyDeclaration,
)
from app.models.network_monitoring import (
    DeviceInterface,
    NetworkDevice,
    NetworkTopologyLink,
    PopSite,
)
from app.models.radius_active_session import RadiusActiveSession
from app.services.audit_adapter import stage_audit_event
from app.services.common import coerce_uuid
from app.services.topology.lldp_poller import SOURCE as LLDP_SOURCE

PATH_KINDS = frozenset({"internal", "border_peer", "nas_termination"})
NODE_ROLES = frozenset(
    {"access", "aggregation", "distribution", "core", "border", "nas"}
)
CONFIGURATION_OWNERS = frozenset(
    {"network.control_plane_intent", "network.routeros_sot"}
)
CONTROL_OBSERVATION_SOURCES = frozenset({"bgp_peer", "routing_table"})
EVIDENCE_STATES = (
    "agreement",
    "missing_observation",
    "drift",
    "invalid_declaration",
)
ACTIVE_DECISION_STATUSES = ("proposed", "approved")
_SHA256_HEX = frozenset("0123456789abcdef")


class ForwardingTopologyError(ValueError):
    """Raised when forwarding declaration or observation evidence is invalid."""


@dataclass(frozen=True)
class ForwardingTopologyPreview:
    action: str
    path_key: str
    declaration_payload: dict[str, object]
    existing_declaration_id: uuid.UUID | None
    expected_topology_sha256: str
    expected_declaration_sha256: str | None
    reason: str
    proposed_by: str
    decision_sha256: str
    existing_decision_id: uuid.UUID | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "decision_sha256": self.decision_sha256,
            "declaration_payload": self.declaration_payload,
            "existing_declaration_id": (
                str(self.existing_declaration_id)
                if self.existing_declaration_id
                else None
            ),
            "existing_decision_id": (
                str(self.existing_decision_id) if self.existing_decision_id else None
            ),
            "expected_declaration_sha256": self.expected_declaration_sha256,
            "expected_topology_sha256": self.expected_topology_sha256,
            "path_key": self.path_key,
            "proposed_by": self.proposed_by,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ForwardingTopologyReport:
    report_sha256: str
    declaration_count: int
    state_counts: dict[str, int]
    graph_blockers: tuple[dict[str, object], ...]
    declarations: tuple[dict[str, object], ...]
    ready_for_operational_projection: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "declaration_count": self.declaration_count,
            "declarations": list(self.declarations),
            "graph_blockers": list(self.graph_blockers),
            "ready_for_operational_projection": (self.ready_for_operational_projection),
            "report_sha256": self.report_sha256,
            "schema_version": 1,
            "state_counts": self.state_counts,
        }


@dataclass(frozen=True)
class ForwardingGraph:
    report_sha256: str
    adjacency: dict[uuid.UUID, frozenset[uuid.UUID]]
    upstream_by_downstream: dict[uuid.UUID, uuid.UUID]
    declaration_by_downstream: dict[uuid.UUID, uuid.UUID]
    root_device_ids: frozenset[uuid.UUID]
    declaration_ids: tuple[uuid.UUID, ...]


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            default=str,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _sha256(value: object, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(char not in _SHA256_HEX for char in normalized):
        raise ForwardingTopologyError(f"{field} must be a SHA-256 value")
    return normalized


def _text(value: object, field: str, *, limit: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ForwardingTopologyError(f"{field} is required")
    if len(normalized) > limit:
        raise ForwardingTopologyError(f"{field} must be at most {limit} characters")
    return normalized


def _optional_text(value: object, field: str, *, limit: int) -> str | None:
    if value is None or not str(value).strip():
        return None
    return _text(value, field, limit=limit)


def _choice(value: object, field: str, choices: frozenset[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in choices:
        raise ForwardingTopologyError(f"{field} is unsupported")
    return normalized


def _uuid(value: object, field: str) -> uuid.UUID:
    try:
        return coerce_uuid(value)
    except (TypeError, ValueError) as exc:
        raise ForwardingTopologyError(f"{field} must be a UUID") from exc


def _optional_uuid(value: object, field: str) -> uuid.UUID | None:
    if value is None or not str(value).strip():
        return None
    return _uuid(value, field)


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ForwardingTopologyError(f"{field} must be a positive integer")
    try:
        normalized = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ForwardingTopologyError(f"{field} must be a positive integer") from exc
    if normalized < 1:
        raise ForwardingTopologyError(f"{field} must be a positive integer")
    return normalized


def _optional_asn(value: object, field: str) -> int | None:
    if value is None or not str(value).strip():
        return None
    normalized = _positive_int(value, field)
    if normalized > 4_294_967_295:
        raise ForwardingTopologyError(f"{field} must be a 32-bit ASN")
    return normalized


def _optional_ip(value: object, field: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError as exc:
        raise ForwardingTopologyError(f"{field} must be an IP address") from exc


def _optional_prefix(value: object, field: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    try:
        return str(ipaddress.ip_network(str(value).strip(), strict=False))
    except ValueError as exc:
        raise ForwardingTopologyError(f"{field} must be an IP prefix") from exc


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _datetime(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        normalized = value
    else:
        try:
            normalized = datetime.fromisoformat(
                str(value or "").strip().replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ForwardingTopologyError(
                f"{field} must be an ISO-8601 timestamp"
            ) from exc
    if normalized.tzinfo is None or normalized.utcoffset() is None:
        raise ForwardingTopologyError(f"{field} must include a timezone")
    return normalized.astimezone(UTC)


def _normalize_payload(value: dict[str, Any]) -> dict[str, object]:
    allowed = {
        "path_key",
        "path_kind",
        "downstream_device_id",
        "downstream_interface_id",
        "downstream_pop_site_id",
        "downstream_role",
        "upstream_device_id",
        "upstream_interface_id",
        "upstream_pop_site_id",
        "upstream_role",
        "vrf_name",
        "peer_ip",
        "peer_asn",
        "route_prefix",
        "next_hop_ip",
        "nas_device_id",
        "preference",
        "configuration_owner",
        "configuration_intent_ref",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ForwardingTopologyError(
            "unsupported forwarding declaration fields: " + ", ".join(sorted(unknown))
        )
    payload: dict[str, object] = {
        "configuration_intent_ref": _text(
            value.get("configuration_intent_ref"),
            "configuration_intent_ref",
            limit=255,
        ),
        "configuration_owner": _choice(
            value.get("configuration_owner"),
            "configuration_owner",
            CONFIGURATION_OWNERS,
        ),
        "downstream_device_id": str(
            _uuid(value.get("downstream_device_id"), "downstream_device_id")
        ),
        "downstream_interface_id": str(
            _uuid(value.get("downstream_interface_id"), "downstream_interface_id")
        ),
        "downstream_pop_site_id": str(
            _uuid(value.get("downstream_pop_site_id"), "downstream_pop_site_id")
        ),
        "downstream_role": _choice(
            value.get("downstream_role"), "downstream_role", NODE_ROLES
        ),
        "nas_device_id": (
            str(normalized)
            if (
                normalized := _optional_uuid(
                    value.get("nas_device_id"), "nas_device_id"
                )
            )
            else None
        ),
        "next_hop_ip": _optional_ip(value.get("next_hop_ip"), "next_hop_ip"),
        "path_key": _text(value.get("path_key"), "path_key", limit=120),
        "path_kind": _choice(value.get("path_kind"), "path_kind", PATH_KINDS),
        "peer_asn": _optional_asn(value.get("peer_asn"), "peer_asn"),
        "peer_ip": _optional_ip(value.get("peer_ip"), "peer_ip"),
        "preference": _positive_int(value.get("preference", 100), "preference"),
        "route_prefix": _optional_prefix(value.get("route_prefix"), "route_prefix"),
        "upstream_device_id": (
            str(normalized)
            if (
                normalized := _optional_uuid(
                    value.get("upstream_device_id"), "upstream_device_id"
                )
            )
            else None
        ),
        "upstream_interface_id": (
            str(normalized)
            if (
                normalized := _optional_uuid(
                    value.get("upstream_interface_id"), "upstream_interface_id"
                )
            )
            else None
        ),
        "upstream_pop_site_id": (
            str(normalized)
            if (
                normalized := _optional_uuid(
                    value.get("upstream_pop_site_id"), "upstream_pop_site_id"
                )
            )
            else None
        ),
        "upstream_role": (
            _choice(value.get("upstream_role"), "upstream_role", NODE_ROLES)
            if value.get("upstream_role") is not None
            and str(value.get("upstream_role")).strip()
            else None
        ),
        "vrf_name": _text(value.get("vrf_name", "main"), "vrf_name", limit=120),
    }
    kind = str(payload["path_kind"])
    upstream_fields = (
        payload["upstream_device_id"],
        payload["upstream_interface_id"],
        payload["upstream_pop_site_id"],
        payload["upstream_role"],
    )
    if kind == "internal":
        if not all(upstream_fields):
            raise ForwardingTopologyError(
                "internal paths require exact upstream device, interface, site, and role"
            )
        if any(
            payload[field] is not None
            for field in (
                "peer_ip",
                "peer_asn",
                "route_prefix",
                "next_hop_ip",
                "nas_device_id",
            )
        ):
            raise ForwardingTopologyError(
                "internal paths cannot declare peer, route, or NAS fields"
            )
    elif kind == "border_peer":
        if any(upstream_fields) or payload["nas_device_id"] is not None:
            raise ForwardingTopologyError(
                "border_peer paths terminate at an external peer, not a Sub device or NAS"
            )
        if payload["downstream_role"] != "border":
            raise ForwardingTopologyError("border_peer downstream_role must be border")
        if not all(
            payload[field] is not None
            for field in ("peer_ip", "peer_asn", "route_prefix", "next_hop_ip")
        ):
            raise ForwardingTopologyError(
                "border_peer paths require exact peer, ASN, route, and next hop"
            )
    else:
        if not all(upstream_fields):
            raise ForwardingTopologyError(
                "nas_termination paths require exact upstream device, interface, site, and role"
            )
        if payload["downstream_role"] != "nas":
            raise ForwardingTopologyError("nas_termination downstream_role must be nas")
        if payload["nas_device_id"] is None:
            raise ForwardingTopologyError(
                "nas_termination paths require exact nas_device_id"
            )
        if payload["peer_ip"] is not None or payload["peer_asn"] is not None:
            raise ForwardingTopologyError(
                "nas_termination paths cannot declare an external BGP peer"
            )
        if payload["route_prefix"] is None or payload["next_hop_ip"] is None:
            raise ForwardingTopologyError(
                "nas_termination paths require exact route and next hop"
            )
    if (
        payload["upstream_device_id"] is not None
        and payload["upstream_device_id"] == payload["downstream_device_id"]
    ):
        raise ForwardingTopologyError("forwarding path devices must be distinct")
    return payload


def _declaration_payload(row: ForwardingTopologyDeclaration) -> dict[str, object]:
    return {
        "configuration_intent_ref": row.configuration_intent_ref,
        "configuration_owner": row.configuration_owner,
        "downstream_device_id": str(row.downstream_device_id),
        "downstream_interface_id": str(row.downstream_interface_id),
        "downstream_pop_site_id": str(row.downstream_pop_site_id),
        "downstream_role": row.downstream_role,
        "nas_device_id": str(row.nas_device_id) if row.nas_device_id else None,
        "next_hop_ip": row.next_hop_ip,
        "path_key": row.path_key,
        "path_kind": row.path_kind,
        "peer_asn": row.peer_asn,
        "peer_ip": row.peer_ip,
        "preference": row.preference,
        "route_prefix": row.route_prefix,
        "upstream_device_id": (
            str(row.upstream_device_id) if row.upstream_device_id else None
        ),
        "upstream_interface_id": (
            str(row.upstream_interface_id) if row.upstream_interface_id else None
        ),
        "upstream_pop_site_id": (
            str(row.upstream_pop_site_id) if row.upstream_pop_site_id else None
        ),
        "upstream_role": row.upstream_role,
        "vrf_name": row.vrf_name,
    }


def _declaration_state(row: ForwardingTopologyDeclaration) -> dict[str, object]:
    return {
        "active": row.active,
        **_declaration_evidence(row),
        "retired_at": _timestamp(row.retired_at) if row.retired_at else None,
        "retired_by_decision_id": (
            str(row.retired_by_decision_id) if row.retired_by_decision_id else None
        ),
    }


def _declaration_evidence(
    row: ForwardingTopologyDeclaration,
) -> dict[str, object]:
    """Return the immutable evidence covered by ``declaration_sha256``.

    Retirement is a separate reviewed transition. Its lifecycle fields belong
    in snapshots and results, but cannot invalidate the original declaration
    hash after a legitimate retirement.
    """

    return {
        "created_by_decision_id": str(row.created_by_decision_id),
        "declaration_id": str(row.id),
        "declaration_payload": _declaration_payload(row),
        "declared_at": _timestamp(row.declared_at),
    }


def _load_device_context(
    db: Session, payload: dict[str, object], *, lock: bool = False
) -> dict[str, object]:
    result: dict[str, object] = {}
    for side in ("downstream", "upstream"):
        raw_device_id = payload.get(f"{side}_device_id")
        if raw_device_id is None:
            continue
        device_id = _uuid(raw_device_id, f"{side}_device_id")
        interface_id = _uuid(
            payload.get(f"{side}_interface_id"), f"{side}_interface_id"
        )
        site_id = _uuid(payload.get(f"{side}_pop_site_id"), f"{side}_pop_site_id")
        device_stmt = select(NetworkDevice).where(NetworkDevice.id == device_id)
        interface_stmt = select(DeviceInterface).where(
            DeviceInterface.id == interface_id
        )
        site_stmt = select(PopSite).where(PopSite.id == site_id)
        if lock:
            device_stmt = device_stmt.with_for_update()
            interface_stmt = interface_stmt.with_for_update()
            site_stmt = site_stmt.with_for_update()
        device = db.scalar(device_stmt)
        interface = db.scalar(interface_stmt)
        site = db.scalar(site_stmt)
        if device is None or not device.is_active:
            raise ForwardingTopologyError(f"{side} device is missing or inactive")
        if interface is None or interface.device_id != device.id:
            raise ForwardingTopologyError(
                f"{side} interface does not belong to the exact device"
            )
        if site is None or not site.is_active or device.pop_site_id != site.id:
            raise ForwardingTopologyError(
                f"{side} site does not match the exact active device projection"
            )
        result[f"{side}_device"] = device
        result[f"{side}_interface"] = interface
        result[f"{side}_site"] = site
    nas_id = payload.get("nas_device_id")
    if nas_id is not None:
        downstream = result.get("downstream_device")
        if not isinstance(downstream, NetworkDevice):
            raise ForwardingTopologyError("NAS path has no downstream device")
        nas: Any = getattr(downstream, "nas_device", None)
        if nas is not None and lock:
            db.refresh(nas, with_for_update=True)
        if (
            nas is None
            or not nas.is_active
            or nas.id != _uuid(nas_id, "nas_device_id")
            or nas.network_device_id != downstream.id
            or nas.pop_site_id != downstream.pop_site_id
        ):
            raise ForwardingTopologyError(
                "NAS does not match the exact active downstream device and site"
            )
        result["nas_device"] = nas
    return result


def _context_state(
    payload: dict[str, object], context: dict[str, object]
) -> dict[str, object]:
    result: dict[str, object] = {"payload": payload}
    for side in ("downstream", "upstream"):
        device = context.get(f"{side}_device")
        interface = context.get(f"{side}_interface")
        site = context.get(f"{side}_site")
        if not isinstance(device, NetworkDevice):
            continue
        assert isinstance(interface, DeviceInterface)
        assert isinstance(site, PopSite)
        result[side] = {
            "device": {
                "id": str(device.id),
                "is_active": device.is_active,
                "legacy_role": getattr(device.role, "value", device.role),
                "pop_site_id": str(device.pop_site_id) if device.pop_site_id else None,
            },
            "interface": {
                "device_id": str(interface.device_id),
                "id": str(interface.id),
                "name": interface.name,
                "status": getattr(interface.status, "value", interface.status),
            },
            "site": {"id": str(site.id), "is_active": site.is_active},
        }
    nas: Any = context.get("nas_device")
    if nas is not None:
        result["nas"] = {
            "id": str(nas.id),
            "is_active": nas.is_active,
            "network_device_id": (
                str(nas.network_device_id) if nas.network_device_id else None
            ),
            "pop_site_id": str(nas.pop_site_id) if nas.pop_site_id else None,
        }
    return result


def _active_declarations(
    db: Session, *, lock: bool = False
) -> list[ForwardingTopologyDeclaration]:
    stmt = (
        select(ForwardingTopologyDeclaration)
        .where(ForwardingTopologyDeclaration.active.is_(True))
        .order_by(
            ForwardingTopologyDeclaration.path_key,
            ForwardingTopologyDeclaration.id,
        )
    )
    if lock:
        stmt = stmt.with_for_update()
    return list(db.scalars(stmt).all())


def _graph_errors(payloads: list[dict[str, object]]) -> list[dict[str, object]]:
    errors: list[dict[str, object]] = []
    path_keys: set[str] = set()
    preferences: set[tuple[str, str, int]] = set()
    roles: dict[str, str] = {}
    sites: dict[str, str] = {}
    parents: dict[tuple[str, str], set[str]] = defaultdict(set)
    for payload in payloads:
        path_key = str(payload["path_key"])
        if path_key in path_keys:
            errors.append({"code": "duplicate_path_key", "path_key": path_key})
        path_keys.add(path_key)
        downstream_id = str(payload["downstream_device_id"])
        vrf = str(payload["vrf_name"])
        preference = _positive_int(payload["preference"], "preference")
        preference_key = (downstream_id, vrf, preference)
        if preference_key in preferences:
            errors.append(
                {
                    "code": "duplicate_downstream_preference",
                    "downstream_device_id": downstream_id,
                    "preference": preference,
                    "vrf_name": vrf,
                }
            )
        preferences.add(preference_key)
        for side in ("downstream", "upstream"):
            device_id = payload.get(f"{side}_device_id")
            if device_id is None:
                continue
            device_key = str(device_id)
            role = str(payload[f"{side}_role"])
            site = str(payload[f"{side}_pop_site_id"])
            if device_key in roles and roles[device_key] != role:
                errors.append(
                    {
                        "code": "conflicting_device_role",
                        "device_id": device_key,
                        "roles": sorted({roles[device_key], role}),
                    }
                )
            roles[device_key] = role
            if device_key in sites and sites[device_key] != site:
                errors.append(
                    {
                        "code": "conflicting_device_site",
                        "device_id": device_key,
                        "sites": sorted({sites[device_key], site}),
                    }
                )
            sites[device_key] = site
        upstream_id = payload.get("upstream_device_id")
        if upstream_id is not None:
            parents[(downstream_id, vrf)].add(str(upstream_id))

    for start, vrf in sorted(parents):
        if _contains_forwarding_cycle(start, vrf, parents):
            errors.append(
                {
                    "code": "forwarding_cycle",
                    "device_id": start,
                    "vrf_name": vrf,
                }
            )
    unique = {_digest(error): error for error in errors}
    return [unique[key] for key in sorted(unique)]


def _contains_forwarding_cycle(
    start: str,
    vrf: str,
    parents: dict[tuple[str, str], set[str]],
) -> bool:
    visited: set[str] = set()
    stack: set[str] = set()

    def walk(device_id: str) -> bool:
        if device_id in stack:
            return True
        if device_id in visited:
            return False
        visited.add(device_id)
        stack.add(device_id)
        if any(walk(upstream) for upstream in parents.get((device_id, vrf), set())):
            return True
        stack.remove(device_id)
        return False

    return walk(start)


def _topology_snapshot(
    db: Session,
    payload: dict[str, object],
    *,
    action: str,
    existing: ForwardingTopologyDeclaration | None,
    lock: bool = False,
) -> tuple[str, dict[str, object]]:
    context = _load_device_context(db, payload, lock=lock)
    declarations = _active_declarations(db, lock=lock)
    active_payloads = [
        _declaration_payload(row)
        for row in declarations
        if existing is None or row.id != existing.id
    ]
    if action == "declare":
        active_payloads.append(payload)
    graph_errors = _graph_errors(active_payloads)
    if graph_errors:
        raise ForwardingTopologyError(
            "forwarding topology invariant failed: "
            + ", ".join(str(item["code"]) for item in graph_errors)
        )
    snapshot = {
        "active_declarations": [_declaration_state(row) for row in declarations],
        "candidate_context": _context_state(payload, context),
        "schema_version": 1,
    }
    return _digest(snapshot), snapshot


def _active_path(db: Session, path_key: str) -> ForwardingTopologyDeclaration | None:
    return db.scalar(
        select(ForwardingTopologyDeclaration).where(
            ForwardingTopologyDeclaration.path_key == path_key,
            ForwardingTopologyDeclaration.active.is_(True),
        )
    )


def preview_forwarding_topology_decision(
    db: Session,
    *,
    action: str,
    declaration: dict[str, Any] | None,
    path_key: object | None,
    reason: object,
    proposed_by: object,
    require_new: bool = False,
) -> ForwardingTopologyPreview:
    """Return a write-free exact declare/retire preview."""

    normalized_action = _choice(action, "action", frozenset({"declare", "retire"}))
    actor = _text(proposed_by, "proposed_by", limit=160)
    normalized_reason = _text(reason, "reason", limit=4000)
    existing: ForwardingTopologyDeclaration | None = None
    if normalized_action == "declare":
        if declaration is None:
            raise ForwardingTopologyError("declare requires declaration payload")
        payload = _normalize_payload(declaration)
        normalized_path_key = str(payload["path_key"])
        if path_key is not None and str(path_key).strip() != normalized_path_key:
            raise ForwardingTopologyError(
                "path_key does not match the declaration payload"
            )
        if _active_path(db, normalized_path_key) is not None:
            raise ForwardingTopologyError(
                "path_key already has an active declaration; retire it first"
            )
        expected_declaration_sha = None
    else:
        if declaration is not None:
            raise ForwardingTopologyError("retire does not accept declaration payload")
        normalized_path_key = _text(path_key, "path_key", limit=120)
        existing = _active_path(db, normalized_path_key)
        if existing is None:
            raise ForwardingTopologyError("active forwarding declaration not found")
        payload = _declaration_payload(existing)
        expected_declaration_sha = existing.declaration_sha256

    topology_sha, _ = _topology_snapshot(
        db,
        payload,
        action=normalized_action,
        existing=existing,
    )
    decision_payload = {
        "action": normalized_action,
        "declaration_payload": payload,
        "existing_declaration_id": str(existing.id) if existing else None,
        "expected_declaration_sha256": expected_declaration_sha,
        "expected_topology_sha256": topology_sha,
        "path_key": normalized_path_key,
        "proposed_by": actor,
        "reason": normalized_reason,
    }
    decision_sha = _digest(decision_payload)
    existing_decision = db.scalar(
        select(ForwardingTopologyDecision).where(
            ForwardingTopologyDecision.decision_sha256 == decision_sha
        )
    )
    if require_new and existing_decision is not None:
        raise ForwardingTopologyError("the exact forwarding decision already exists")
    overlap = db.scalar(
        select(ForwardingTopologyDecision).where(
            ForwardingTopologyDecision.path_key == normalized_path_key,
            ForwardingTopologyDecision.status.in_(ACTIVE_DECISION_STATUSES),
            *(
                (ForwardingTopologyDecision.id != existing_decision.id,)
                if existing_decision is not None
                else ()
            ),
        )
    )
    if overlap is not None:
        raise ForwardingTopologyError(
            "an active forwarding decision already covers this path_key"
        )
    return ForwardingTopologyPreview(
        action=normalized_action,
        path_key=normalized_path_key,
        declaration_payload=payload,
        existing_declaration_id=existing.id if existing else None,
        expected_topology_sha256=topology_sha,
        expected_declaration_sha256=expected_declaration_sha,
        reason=normalized_reason,
        proposed_by=actor,
        decision_sha256=decision_sha,
        existing_decision_id=existing_decision.id if existing_decision else None,
    )


def propose_forwarding_topology_decision(
    db: Session,
    *,
    expected_decision_sha256: object,
    commit: bool = True,
    **preview_args: Any,
) -> ForwardingTopologyDecision:
    expected = _sha256(expected_decision_sha256, "expected_decision_sha256")
    preview = preview_forwarding_topology_decision(db, **preview_args)
    if preview.decision_sha256 != expected:
        raise ForwardingTopologyError("forwarding decision preview changed")
    if preview.existing_decision_id is not None:
        existing = db.get(ForwardingTopologyDecision, preview.existing_decision_id)
        if existing is not None:
            return existing
    row = ForwardingTopologyDecision(
        action=preview.action,
        path_key=preview.path_key,
        declaration_payload=preview.declaration_payload,
        existing_declaration_id=preview.existing_declaration_id,
        expected_topology_sha256=preview.expected_topology_sha256,
        expected_declaration_sha256=preview.expected_declaration_sha256,
        reason=preview.reason,
        proposed_by=preview.proposed_by,
        status="proposed",
        decision_sha256=preview.decision_sha256,
    )
    db.add(row)
    db.flush()
    _audit(db, row, "forwarding_topology.proposed", preview.proposed_by)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def _load_decision(
    db: Session, decision_id: object, *, lock: bool = False
) -> ForwardingTopologyDecision:
    normalized = _uuid(decision_id, "decision_id")
    stmt = select(ForwardingTopologyDecision).where(
        ForwardingTopologyDecision.id == normalized
    )
    if lock:
        stmt = stmt.with_for_update()
    row = db.scalar(stmt)
    if row is None:
        raise ForwardingTopologyError("forwarding topology decision not found")
    return row


def _decision_preview(
    db: Session, row: ForwardingTopologyDecision, *, lock: bool = False
) -> ForwardingTopologyPreview:
    payload = _normalize_payload(dict(row.declaration_payload or {}))
    existing: ForwardingTopologyDeclaration | None = None
    if row.action == "declare":
        if _active_path(db, row.path_key) is not None:
            raise ForwardingTopologyError("path_key now has an active declaration")
    else:
        if row.existing_declaration_id is None:
            raise ForwardingTopologyError("retire decision has no declaration")
        stmt = select(ForwardingTopologyDeclaration).where(
            ForwardingTopologyDeclaration.id == row.existing_declaration_id
        )
        if lock:
            stmt = stmt.with_for_update()
        existing = db.scalar(stmt)
        if (
            existing is None
            or not existing.active
            or existing.path_key != row.path_key
            or existing.declaration_sha256 != row.expected_declaration_sha256
            or _declaration_payload(existing) != payload
        ):
            raise ForwardingTopologyError("forwarding declaration evidence changed")
    topology_sha, _ = _topology_snapshot(
        db,
        payload,
        action=row.action,
        existing=existing,
        lock=lock,
    )
    if topology_sha != row.expected_topology_sha256:
        raise ForwardingTopologyError("forwarding topology evidence changed")
    decision_payload = {
        "action": row.action,
        "declaration_payload": payload,
        "existing_declaration_id": str(existing.id) if existing else None,
        "expected_declaration_sha256": row.expected_declaration_sha256,
        "expected_topology_sha256": topology_sha,
        "path_key": row.path_key,
        "proposed_by": row.proposed_by,
        "reason": row.reason,
    }
    if _digest(decision_payload) != row.decision_sha256:
        raise ForwardingTopologyError("forwarding decision evidence is invalid")
    return ForwardingTopologyPreview(
        action=row.action,
        path_key=row.path_key,
        declaration_payload=payload,
        existing_declaration_id=existing.id if existing else None,
        expected_topology_sha256=topology_sha,
        expected_declaration_sha256=row.expected_declaration_sha256,
        reason=row.reason,
        proposed_by=row.proposed_by,
        decision_sha256=row.decision_sha256,
        existing_decision_id=row.id,
    )


def review_forwarding_topology_decision(
    db: Session,
    decision_id: object,
    *,
    action: str,
    reviewed_by: object,
    review_notes: object,
    expected_decision_sha256: object,
    commit: bool = True,
) -> ForwardingTopologyDecision:
    expected = _sha256(expected_decision_sha256, "expected_decision_sha256")
    actor = _text(reviewed_by, "reviewed_by", limit=160)
    notes = _text(review_notes, "review_notes", limit=4000)
    normalized_action = _choice(
        action, "review action", frozenset({"approve", "decline"})
    )
    row = _load_decision(db, decision_id, lock=True)
    if row.decision_sha256 != expected:
        raise ForwardingTopologyError("forwarding decision confirmation is stale")
    target_status = "approved" if normalized_action == "approve" else "declined"
    if (
        row.status == target_status
        and row.reviewed_by == actor
        and row.review_notes == notes
    ):
        return row
    if row.status != "proposed":
        raise ForwardingTopologyError("forwarding decision is not awaiting review")
    if row.proposed_by == actor:
        raise ForwardingTopologyError(
            "the proposer cannot review this forwarding decision"
        )
    if target_status == "approved":
        _decision_preview(db, row, lock=True)
    row.status = target_status
    row.reviewed_by = actor
    row.review_notes = notes
    row.reviewed_at = datetime.now(UTC)
    if target_status == "declined":
        row.closed_reason = "forwarding_topology_decision_declined"
    _audit(db, row, f"forwarding_topology.{target_status}", actor)
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def _set_result(
    row: ForwardingTopologyDecision,
    *,
    actor: str,
    status: str,
    payload: dict[str, object],
    declaration_id: uuid.UUID | None = None,
    closed_reason: str | None = None,
) -> None:
    row.status = status
    row.executed_by = actor
    row.executed_at = datetime.now(UTC)
    row.closed_reason = closed_reason
    row.result_declaration_id = declaration_id
    row.result_payload = payload
    row.result_sha256 = _digest(payload)


def _finish_execution(
    db: Session,
    row: ForwardingTopologyDecision,
    *,
    actor: str,
    commit: bool,
) -> ForwardingTopologyDecision:
    _audit(
        db,
        row,
        f"forwarding_topology.{row.status}",
        actor,
        metadata={
            "result": row.result_payload,
            "result_sha256": row.result_sha256,
        },
    )
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def execute_forwarding_topology_decision(
    db: Session,
    decision_id: object,
    *,
    executed_by: object,
    expected_decision_sha256: object,
    commit: bool = True,
) -> ForwardingTopologyDecision:
    """Lock, revalidate, and apply the exact reviewed declaration transition."""

    expected = _sha256(expected_decision_sha256, "expected_decision_sha256")
    actor = _text(executed_by, "executed_by", limit=160)
    row = _load_decision(db, decision_id, lock=True)
    if row.decision_sha256 != expected:
        raise ForwardingTopologyError("forwarding decision confirmation is stale")
    if row.status in {"applied", "closed"}:
        return row
    if row.status != "approved":
        raise ForwardingTopologyError("forwarding decision is not approved")
    try:
        preview = _decision_preview(db, row, lock=True)
    except ForwardingTopologyError as exc:
        result = {
            "action": row.action,
            "decision_id": str(row.id),
            "error": str(exc),
            "executed_by": actor,
            "outcome": "closed_stale",
            "path_key": row.path_key,
            "schema_version": 1,
        }
        _set_result(
            row,
            actor=actor,
            status="closed",
            payload=result,
            closed_reason="authoritative_forwarding_inputs_changed",
        )
        return _finish_execution(db, row, actor=actor, commit=commit)

    now = datetime.now(UTC)
    declaration: ForwardingTopologyDeclaration | None
    if row.action == "declare":
        values = preview.declaration_payload
        declaration = ForwardingTopologyDeclaration(
            path_key=row.path_key,
            path_kind=str(values["path_kind"]),
            downstream_device_id=_uuid(
                values["downstream_device_id"], "downstream_device_id"
            ),
            downstream_interface_id=_uuid(
                values["downstream_interface_id"], "downstream_interface_id"
            ),
            downstream_pop_site_id=_uuid(
                values["downstream_pop_site_id"], "downstream_pop_site_id"
            ),
            downstream_role=str(values["downstream_role"]),
            upstream_device_id=_optional_uuid(
                values["upstream_device_id"], "upstream_device_id"
            ),
            upstream_interface_id=_optional_uuid(
                values["upstream_interface_id"], "upstream_interface_id"
            ),
            upstream_pop_site_id=_optional_uuid(
                values["upstream_pop_site_id"], "upstream_pop_site_id"
            ),
            upstream_role=(
                str(values["upstream_role"])
                if values["upstream_role"] is not None
                else None
            ),
            vrf_name=str(values["vrf_name"]),
            peer_ip=str(values["peer_ip"]) if values["peer_ip"] else None,
            peer_asn=(
                _positive_int(values["peer_asn"], "peer_asn")
                if values["peer_asn"]
                else None
            ),
            route_prefix=(
                str(values["route_prefix"]) if values["route_prefix"] else None
            ),
            next_hop_ip=(str(values["next_hop_ip"]) if values["next_hop_ip"] else None),
            nas_device_id=_optional_uuid(values["nas_device_id"], "nas_device_id"),
            preference=_positive_int(values["preference"], "preference"),
            configuration_owner=str(values["configuration_owner"]),
            configuration_intent_ref=str(values["configuration_intent_ref"]),
            created_by_decision_id=row.id,
            declaration_sha256="",
            active=True,
            declared_at=now,
        )
        declaration.id = uuid.uuid4()
        declaration.declaration_sha256 = _digest(_declaration_evidence(declaration))
        try:
            with db.begin_nested():
                db.add(declaration)
                db.flush()
        except IntegrityError:
            result = {
                "action": row.action,
                "decision_id": str(row.id),
                "error": "canonical forwarding declaration uniqueness conflict",
                "executed_by": actor,
                "outcome": "closed_conflict",
                "path_key": row.path_key,
                "schema_version": 1,
            }
            _set_result(
                row,
                actor=actor,
                status="closed",
                payload=result,
                closed_reason="canonical_forwarding_declaration_conflict",
            )
            return _finish_execution(db, row, actor=actor, commit=commit)
    else:
        assert row.existing_declaration_id is not None
        declaration = db.scalar(
            select(ForwardingTopologyDeclaration)
            .where(ForwardingTopologyDeclaration.id == row.existing_declaration_id)
            .with_for_update()
        )
        if declaration is None:
            raise ForwardingTopologyError("forwarding declaration disappeared")
        declaration.active = False
        declaration.retired_by_decision_id = row.id
        declaration.retired_at = now
        db.flush()
    if declaration is None:
        raise ForwardingTopologyError("forwarding declaration disappeared")
    result = {
        "action": row.action,
        "declaration": _declaration_state(declaration),
        "declaration_sha256": declaration.declaration_sha256,
        "decision_id": str(row.id),
        "executed_by": actor,
        "outcome": "applied",
        "path_key": row.path_key,
        "schema_version": 1,
    }
    _set_result(
        row,
        actor=actor,
        status="applied",
        payload=result,
        declaration_id=declaration.id,
    )
    return _finish_execution(db, row, actor=actor, commit=commit)


def record_forwarding_control_observation(
    db: Session,
    *,
    client_ref: object,
    source_type: object,
    collector: object,
    collector_run_id: object,
    device_id: object,
    interface_id: object,
    vrf_name: object,
    peer_ip: object | None,
    peer_asn: object | None,
    route_prefix: object | None,
    next_hop_ip: object | None,
    source_evidence_sha256: object,
    observed_at: object,
    expires_at: object,
    commit: bool = True,
) -> ForwardingControlObservation:
    """Append one normalized BGP or route fact without deciding topology."""

    normalized_client_ref = _uuid(client_ref, "client_ref")
    normalized_source = _choice(source_type, "source_type", CONTROL_OBSERVATION_SOURCES)
    normalized_device_id = _uuid(device_id, "device_id")
    normalized_interface_id = _uuid(interface_id, "interface_id")
    device = db.get(NetworkDevice, normalized_device_id)
    interface = db.get(DeviceInterface, normalized_interface_id)
    if device is None or not device.is_active:
        raise ForwardingTopologyError("observation device is missing or inactive")
    if interface is None or interface.device_id != device.id:
        raise ForwardingTopologyError(
            "observation interface does not belong to the exact device"
        )
    normalized_observed_at = _datetime(observed_at, "observed_at")
    normalized_expires_at = _datetime(expires_at, "expires_at")
    if normalized_expires_at <= normalized_observed_at:
        raise ForwardingTopologyError("expires_at must be after observed_at")
    payload = {
        "collector": _text(collector, "collector", limit=120),
        "collector_run_id": _text(collector_run_id, "collector_run_id", limit=160),
        "device_id": str(normalized_device_id),
        "expires_at": _timestamp(normalized_expires_at),
        "interface_id": str(normalized_interface_id),
        "next_hop_ip": _optional_ip(next_hop_ip, "next_hop_ip"),
        "observed_at": _timestamp(normalized_observed_at),
        "peer_asn": _optional_asn(peer_asn, "peer_asn"),
        "peer_ip": _optional_ip(peer_ip, "peer_ip"),
        "route_prefix": _optional_prefix(route_prefix, "route_prefix"),
        "source_evidence_sha256": _sha256(
            source_evidence_sha256, "source_evidence_sha256"
        ),
        "source_type": normalized_source,
        "vrf_name": _text(vrf_name, "vrf_name", limit=120),
    }
    if normalized_source == "bgp_peer":
        if payload["peer_ip"] is None or payload["peer_asn"] is None:
            raise ForwardingTopologyError("BGP observations require peer IP and ASN")
        if payload["route_prefix"] is not None or payload["next_hop_ip"] is not None:
            raise ForwardingTopologyError(
                "BGP observations cannot carry routing-table fields"
            )
    else:
        if payload["route_prefix"] is None or payload["next_hop_ip"] is None:
            raise ForwardingTopologyError(
                "routing observations require prefix and next hop"
            )
        if payload["peer_ip"] is not None or payload["peer_asn"] is not None:
            raise ForwardingTopologyError(
                "routing observations cannot carry BGP peer fields"
            )
    observation_sha = _digest(payload)
    by_client_ref = db.scalar(
        select(ForwardingControlObservation).where(
            ForwardingControlObservation.client_ref == normalized_client_ref
        )
    )
    if by_client_ref is not None:
        if by_client_ref.observation_sha256 != observation_sha:
            raise ForwardingTopologyError(
                "client_ref already identifies a different forwarding observation"
            )
        return by_client_ref
    existing = db.scalar(
        select(ForwardingControlObservation).where(
            ForwardingControlObservation.observation_sha256 == observation_sha
        )
    )
    if existing is not None:
        return existing
    row = ForwardingControlObservation(
        client_ref=normalized_client_ref,
        source_type=normalized_source,
        collector=str(payload["collector"]),
        collector_run_id=str(payload["collector_run_id"]),
        device_id=normalized_device_id,
        interface_id=normalized_interface_id,
        vrf_name=str(payload["vrf_name"]),
        peer_ip=str(payload["peer_ip"]) if payload["peer_ip"] else None,
        peer_asn=int(payload["peer_asn"]) if payload["peer_asn"] else None,
        route_prefix=(
            str(payload["route_prefix"]) if payload["route_prefix"] else None
        ),
        next_hop_ip=(str(payload["next_hop_ip"]) if payload["next_hop_ip"] else None),
        source_evidence_sha256=str(payload["source_evidence_sha256"]),
        observed_at=normalized_observed_at,
        expires_at=normalized_expires_at,
        observation_sha256=observation_sha,
    )
    db.add(row)
    db.flush()
    if commit:
        db.commit()
        db.refresh(row)
    return row


def _same_lldp_endpoints(
    link: NetworkTopologyLink, declaration: ForwardingTopologyDeclaration
) -> bool:
    expected = {
        (declaration.downstream_device_id, declaration.downstream_interface_id),
        (declaration.upstream_device_id, declaration.upstream_interface_id),
    }
    observed = {
        (link.source_device_id, link.source_interface_id),
        (link.target_device_id, link.target_interface_id),
    }
    return expected == observed


def _uses_declared_lldp_endpoint(
    link: NetworkTopologyLink,
    declaration: ForwardingTopologyDeclaration,
) -> bool:
    observed = {
        (link.source_device_id, link.source_interface_id),
        (link.target_device_id, link.target_interface_id),
    }
    return bool(
        observed
        & {
            (
                declaration.downstream_device_id,
                declaration.downstream_interface_id,
            ),
            (declaration.upstream_device_id, declaration.upstream_interface_id),
        }
    )


def _current_context_valid(
    declaration: ForwardingTopologyDeclaration,
    devices: dict[uuid.UUID, NetworkDevice],
    interfaces: dict[uuid.UUID, DeviceInterface],
    sites: dict[uuid.UUID, PopSite],
) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    expected_hash = _digest(_declaration_evidence(declaration))
    if expected_hash != declaration.declaration_sha256:
        blockers.append("declaration_evidence_drift")
    for side in ("downstream", "upstream"):
        device_id = getattr(declaration, f"{side}_device_id")
        if device_id is None:
            continue
        interface_id = getattr(declaration, f"{side}_interface_id")
        site_id = getattr(declaration, f"{side}_pop_site_id")
        device = devices.get(device_id)
        interface = interfaces.get(interface_id)
        site = sites.get(site_id)
        if device is None or not device.is_active:
            blockers.append(f"{side}_device_missing_or_inactive")
        elif device.pop_site_id != site_id:
            blockers.append(f"{side}_site_projection_drift")
        if interface is None or interface.device_id != device_id:
            blockers.append(f"{side}_interface_projection_drift")
        if site is None or not site.is_active:
            blockers.append(f"{side}_site_missing_or_inactive")
    if declaration.nas_device_id is not None:
        downstream = devices.get(declaration.downstream_device_id)
        nas = getattr(downstream, "nas_device", None) if downstream else None
        if (
            nas is None
            or not nas.is_active
            or nas.network_device_id != declaration.downstream_device_id
            or nas.pop_site_id != declaration.downstream_pop_site_id
        ):
            blockers.append("nas_termination_projection_drift")
    return not blockers, blockers


def reconcile_forwarding_topology(
    db: Session, *, as_of: datetime | None = None
) -> ForwardingTopologyReport:
    """Idempotently derive declaration agreement/drift without writing state."""

    evaluated_at = as_of or datetime.now(UTC)
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        evaluated_at = evaluated_at.replace(tzinfo=UTC)
    declarations = _active_declarations(db)
    device_ids = {
        value
        for declaration in declarations
        for value in (
            declaration.downstream_device_id,
            declaration.upstream_device_id,
        )
        if value is not None
    }
    interface_ids = {
        value
        for declaration in declarations
        for value in (
            declaration.downstream_interface_id,
            declaration.upstream_interface_id,
        )
        if value is not None
    }
    site_ids = {
        value
        for declaration in declarations
        for value in (
            declaration.downstream_pop_site_id,
            declaration.upstream_pop_site_id,
        )
        if value is not None
    }
    nas_ids = {
        declaration.nas_device_id
        for declaration in declarations
        if declaration.nas_device_id is not None
    }
    devices = {
        row.id: row
        for row in (
            db.scalars(select(NetworkDevice).where(NetworkDevice.id.in_(device_ids)))
            if device_ids
            else []
        )
    }
    interfaces = {
        row.id: row
        for row in (
            db.scalars(
                select(DeviceInterface).where(DeviceInterface.id.in_(interface_ids))
            )
            if interface_ids
            else []
        )
    }
    sites = {
        row.id: row
        for row in (
            db.scalars(select(PopSite).where(PopSite.id.in_(site_ids)))
            if site_ids
            else []
        )
    }
    lldp_links = list(
        db.scalars(
            select(NetworkTopologyLink).where(
                NetworkTopologyLink.source == LLDP_SOURCE,
                NetworkTopologyLink.is_active.is_(True),
            )
        ).all()
    )
    control_observations = list(
        db.scalars(
            select(ForwardingControlObservation).where(
                ForwardingControlObservation.observed_at <= evaluated_at,
                ForwardingControlObservation.expires_at > evaluated_at,
            )
        ).all()
    )
    radius_counts: dict[uuid.UUID, int] = {}
    if nas_ids:
        radius_counts = {
            nas_id: count
            for nas_id, count in db.execute(
                select(
                    RadiusActiveSession.nas_device_id,
                    func.count(RadiusActiveSession.id),
                )
                .where(RadiusActiveSession.nas_device_id.in_(nas_ids))
                .group_by(RadiusActiveSession.nas_device_id)
            )
            .tuples()
            .all()
            if nas_id is not None
        }

    result_rows: list[dict[str, object]] = []
    states: list[str] = []
    for declaration in declarations:
        context_valid, context_blockers = _current_context_valid(
            declaration, devices, interfaces, sites
        )
        requirements = (
            ("lldp",)
            if declaration.path_kind == "internal"
            else (
                ("bgp_peer", "routing_table")
                if declaration.path_kind == "border_peer"
                else ("lldp", "routing_table")
            )
        )
        evidence: dict[str, dict[str, object]] = {}
        missing: list[str] = []
        drift: list[str] = []
        if "lldp" in requirements:
            exact_lldp = [
                link for link in lldp_links if _same_lldp_endpoints(link, declaration)
            ]
            conflicts = [
                link
                for link in lldp_links
                if link not in exact_lldp
                and _uses_declared_lldp_endpoint(link, declaration)
            ]
            evidence["lldp"] = {
                "conflict_link_ids": [str(link.id) for link in conflicts],
                "exact_link_ids": [str(link.id) for link in exact_lldp],
            }
            if conflicts:
                drift.append("lldp")
            elif not exact_lldp:
                missing.append("lldp")
        for source_type in ("bgp_peer", "routing_table"):
            interface_observations = [
                row
                for row in control_observations
                if row.source_type == source_type
                and row.device_id == declaration.downstream_device_id
                and row.interface_id == declaration.downstream_interface_id
                and row.vrf_name == declaration.vrf_name
            ]
            if source_type == "bgp_peer":
                candidates = [
                    row
                    for row in interface_observations
                    if row.peer_ip == declaration.peer_ip
                    or row.peer_asn == declaration.peer_asn
                ]
                exact_control = [
                    row
                    for row in candidates
                    if row.peer_ip == declaration.peer_ip
                    and row.peer_asn == declaration.peer_asn
                ]
            else:
                candidates = [
                    row
                    for row in interface_observations
                    if row.route_prefix == declaration.route_prefix
                ]
                exact_control = [
                    row
                    for row in candidates
                    if row.route_prefix == declaration.route_prefix
                    and row.next_hop_ip == declaration.next_hop_ip
                ]
            conflicting_control = [
                row for row in candidates if row not in exact_control
            ]
            if source_type in requirements:
                evidence[source_type] = {
                    "conflict_observation_ids": [
                        str(row.id) for row in conflicting_control
                    ],
                    "exact_observation_ids": [str(row.id) for row in exact_control],
                }
                if conflicting_control:
                    drift.append(source_type)
                elif not exact_control:
                    missing.append(source_type)
        if declaration.nas_device_id is not None:
            evidence["radius_sessions"] = {
                "active_session_count": radius_counts.get(declaration.nas_device_id, 0),
                "authority": "online_session_observation_only",
            }
        if not context_valid:
            state = "invalid_declaration"
        elif drift:
            state = "drift"
        elif missing:
            state = "missing_observation"
        else:
            state = "agreement"
        states.append(state)
        result_rows.append(
            {
                "configuration_intent_ref": declaration.configuration_intent_ref,
                "configuration_owner": declaration.configuration_owner,
                "context_blocker_codes": sorted(context_blockers),
                "declaration_id": str(declaration.id),
                "declaration_payload": _declaration_payload(declaration),
                "declaration_sha256": declaration.declaration_sha256,
                "drift_sources": sorted(drift),
                "evidence": evidence,
                "evidence_state": state,
                "missing_sources": sorted(missing),
                "path_key": declaration.path_key,
                "required_observation_sources": list(requirements),
            }
        )
    graph_blockers = tuple(
        _graph_errors([_declaration_payload(row) for row in declarations])
    )
    state_counter = Counter(states)
    state_counts = {state: state_counter[state] for state in EVIDENCE_STATES}
    ready = (
        bool(declarations)
        and not graph_blockers
        and all(state == "agreement" for state in states)
    )
    report_payload: dict[str, object] = {
        "declaration_count": len(declarations),
        "declarations": result_rows,
        "graph_blockers": list(graph_blockers),
        "ready_for_operational_projection": ready,
        "schema_version": 1,
        "state_counts": state_counts,
    }
    return ForwardingTopologyReport(
        report_sha256=_digest(report_payload),
        declaration_count=len(declarations),
        state_counts=state_counts,
        graph_blockers=graph_blockers,
        declarations=tuple(result_rows),
        ready_for_operational_projection=ready,
    )


def _report_row_preference(row: dict[str, object]) -> int:
    payload = row.get("declaration_payload")
    if not isinstance(payload, dict):
        raise ForwardingTopologyError("forwarding report declaration is invalid")
    return _positive_int(payload.get("preference"), "preference")


def project_authoritative_forwarding_graph(
    db: Session, *, vrf_name: str = "main"
) -> ForwardingGraph:
    """Return only declared primary edges with current exact observation agreement."""

    normalized_vrf = _text(vrf_name, "vrf_name", limit=120)
    report = reconcile_forwarding_topology(db)
    agreeing = [
        row
        for row in report.declarations
        if row["evidence_state"] == "agreement"
        and isinstance(row["declaration_payload"], dict)
        and row["declaration_payload"].get("vrf_name") == normalized_vrf
    ]
    by_downstream: dict[str, list[dict[str, object]]] = defaultdict(list)
    roots: set[uuid.UUID] = set()
    for row in agreeing:
        payload = row["declaration_payload"]
        assert isinstance(payload, dict)
        downstream_id = str(payload["downstream_device_id"])
        if payload["downstream_role"] in {"core", "border"}:
            roots.add(_uuid(downstream_id, "downstream_device_id"))
        upstream_id = payload.get("upstream_device_id")
        if upstream_id is not None:
            by_downstream[downstream_id].append(row)
            if payload.get("upstream_role") in {"core", "border"}:
                roots.add(_uuid(upstream_id, "upstream_device_id"))
    upstream_by_downstream: dict[uuid.UUID, uuid.UUID] = {}
    declaration_by_downstream: dict[uuid.UUID, uuid.UUID] = {}
    adjacency_mutable: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    declaration_ids: list[uuid.UUID] = []
    for downstream_id, candidates in by_downstream.items():
        candidates.sort(
            key=lambda row: (
                _report_row_preference(row),
                str(row["declaration_id"]),
            )
        )
        selected = candidates[0]
        payload = selected["declaration_payload"]
        assert isinstance(payload, dict)
        downstream = _uuid(downstream_id, "downstream_device_id")
        upstream = _uuid(payload["upstream_device_id"], "upstream_device_id")
        upstream_by_downstream[downstream] = upstream
        declaration_by_downstream[downstream] = _uuid(
            selected["declaration_id"], "declaration_id"
        )
        adjacency_mutable[downstream].add(upstream)
        adjacency_mutable[upstream].add(downstream)
        declaration_ids.append(_uuid(selected["declaration_id"], "declaration_id"))
    return ForwardingGraph(
        report_sha256=report.report_sha256,
        adjacency={key: frozenset(value) for key, value in adjacency_mutable.items()},
        upstream_by_downstream=upstream_by_downstream,
        declaration_by_downstream=declaration_by_downstream,
        root_device_ids=frozenset(roots),
        declaration_ids=tuple(sorted(declaration_ids, key=str)),
    )


def resolve_authoritative_upstream_chain(
    db: Session,
    access_device_id: object,
    *,
    vrf_name: str = "main",
    maximum_hops: int = 16,
) -> list[NetworkDevice]:
    """Resolve the exact declared and observation-agreeing path toward core/border."""

    start = _uuid(access_device_id, "access_device_id")
    graph = project_authoritative_forwarding_graph(db, vrf_name=vrf_name)
    chain_ids: list[uuid.UUID] = []
    seen = {start}
    current = start
    while len(chain_ids) < maximum_hops:
        upstream = graph.upstream_by_downstream.get(current)
        if upstream is None or upstream in seen:
            break
        chain_ids.append(upstream)
        seen.add(upstream)
        if upstream in graph.root_device_ids:
            break
        current = upstream
    if not chain_ids or chain_ids[-1] not in graph.root_device_ids:
        return []
    devices = {
        row.id: row
        for row in db.scalars(
            select(NetworkDevice).where(NetworkDevice.id.in_(chain_ids))
        ).all()
    }
    return [devices[device_id] for device_id in chain_ids if device_id in devices]


def inspect_forwarding_topology_decision(
    db: Session, decision_id: object
) -> dict[str, object]:
    row = _load_decision(db, decision_id)
    result_valid = (
        None
        if row.result_payload is None and row.result_sha256 is None
        else bool(
            row.result_payload is not None
            and row.result_sha256 is not None
            and _digest(row.result_payload) == row.result_sha256
        )
    )
    return {
        "action": row.action,
        "decision_id": str(row.id),
        "decision_sha256": row.decision_sha256,
        "declaration_payload": row.declaration_payload,
        "executed_by": row.executed_by,
        "path_key": row.path_key,
        "proposed_by": row.proposed_by,
        "result_declaration_id": (
            str(row.result_declaration_id) if row.result_declaration_id else None
        ),
        "result_payload": row.result_payload,
        "result_sha256": row.result_sha256,
        "result_valid": result_valid,
        "reviewed_by": row.reviewed_by,
        "status": row.status,
    }


def _audit(
    db: Session,
    row: ForwardingTopologyDecision,
    action: str,
    actor: str,
    *,
    metadata: dict[str, object] | None = None,
) -> None:
    stage_audit_event(
        db,
        action=action,
        entity_type="forwarding_topology_decision",
        entity_id=str(row.id),
        actor_type=AuditActorType.system,
        metadata={
            "actor": actor,
            "decision_sha256": row.decision_sha256,
            "owner": "network.forwarding_topology",
            "path_key": row.path_key,
            "status": row.status,
            **(metadata or {}),
        },
    )


__all__ = [
    "CONFIGURATION_OWNERS",
    "CONTROL_OBSERVATION_SOURCES",
    "EVIDENCE_STATES",
    "NODE_ROLES",
    "PATH_KINDS",
    "ForwardingGraph",
    "ForwardingTopologyError",
    "ForwardingTopologyPreview",
    "ForwardingTopologyReport",
    "execute_forwarding_topology_decision",
    "inspect_forwarding_topology_decision",
    "preview_forwarding_topology_decision",
    "project_authoritative_forwarding_graph",
    "propose_forwarding_topology_decision",
    "reconcile_forwarding_topology",
    "record_forwarding_control_observation",
    "resolve_authoritative_upstream_chain",
    "review_forwarding_topology_decision",
]
