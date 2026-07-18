"""Read-only RouterOS collection for forwarding-control observations.

The collector is a transport adapter.  It reads only the exact devices,
interfaces, peers, VRFs, and prefixes already selected by reviewed forwarding
declarations, then submits normalized expiring facts to
``network.forwarding_topology``.  It never declares topology or applies router
configuration.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.forwarding_topology import ForwardingTopologyDeclaration
from app.models.network_monitoring import DeviceInterface
from app.models.router_management import Router
from app.services.network.forwarding_topology import (
    record_forwarding_control_observation,
)
from app.services.router_management.connection import RouterConnectionService

COLLECTOR_NAME = "routeros:forwarding-control-v1"
COLLECTOR_NAMESPACE = uuid.UUID("70d6851e-924c-45bf-b1c8-70696c588660")
DEFAULT_OBSERVATION_TTL_SECONDS = 900
MINIMUM_OBSERVATION_TTL_SECONDS = 300


class ForwardingObservationReader(Protocol):
    """Read-only RouterOS data needed by the normalized collector."""

    def bgp_sessions(self, router: Router) -> list[dict[str, object]]: ...

    def interface_addresses(
        self, router: Router, *, family: int
    ) -> list[dict[str, object]]: ...

    def routes(self, router: Router, *, prefix: str) -> list[dict[str, object]]: ...


class RouterOSForwardingObservationReader:
    """Fail-fast GET-only RouterOS REST reader."""

    _BGP_PROPERTIES = (
        ".id",
        "name",
        "established",
        "state",
        "remote.address",
        "remote.as",
        "local.address",
        "routing-table",
        "vrf",
    )
    _ADDRESS_PROPERTIES = (
        ".id",
        "address",
        "interface",
        "disabled",
        "invalid",
    )
    _ROUTE_PROPERTIES = (
        ".id",
        "active",
        "disabled",
        "filtered",
        "unreachable",
        "dst-address",
        "routing-table",
        "immediate-gw",
    )

    @staticmethod
    def _read_list(router: Router, path: str) -> list[dict[str, object]]:
        response = RouterConnectionService.require_list_response(
            RouterConnectionService.execute(
                router,
                "GET",
                path,
                connect_timeout=10,
                read_timeout=20,
                max_retries=1,
            ),
            path,
        )
        rows: list[dict[str, object]] = []
        for value in response:
            if not isinstance(value, dict):
                raise RuntimeError(
                    f"RouterOS endpoint {path} returned a non-object record"
                )
            rows.append(dict(value))
        return rows

    @staticmethod
    def _path(resource: str, properties: tuple[str, ...], **filters: str) -> str:
        query = {".proplist": ",".join(properties), **filters}
        return f"{resource}?{urlencode(query)}"

    def bgp_sessions(self, router: Router) -> list[dict[str, object]]:
        return self._read_list(
            router,
            self._path("/routing/bgp/session", self._BGP_PROPERTIES),
        )

    def interface_addresses(
        self, router: Router, *, family: int
    ) -> list[dict[str, object]]:
        if family not in {4, 6}:
            raise ValueError("address family must be 4 or 6")
        resource = "/ip/address" if family == 4 else "/ipv6/address"
        return self._read_list(
            router,
            self._path(resource, self._ADDRESS_PROPERTIES),
        )

    def routes(self, router: Router, *, prefix: str) -> list[dict[str, object]]:
        return self._read_list(
            router,
            self._path(
                "/routing/route",
                self._ROUTE_PROPERTIES,
                **{"dst-address": prefix},
            ),
        )


def _value(row: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _true(value: object) -> bool:
    return str(value or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "established",
    }


def _false(value: object) -> bool:
    return str(value or "").strip().lower() in {"", "0", "false", "no", "off"}


def _ip_and_zone(value: object) -> tuple[str, str | None]:
    text = str(value or "").strip()
    if not text:
        raise ValueError("missing IP address")
    if "/" in text:
        text = text.split("/", 1)[0]
    zone = None
    if "%" in text:
        text, zone = text.rsplit("%", 1)
        zone = zone.strip() or None
    return str(ipaddress.ip_address(text.strip())), zone


def _prefix(value: object) -> str:
    return str(ipaddress.ip_network(str(value or "").strip(), strict=False))


def _asn(value: object) -> int:
    text = str(value or "").strip()
    if not text:
        raise ValueError("missing ASN")
    if "." in text:
        high_text, low_text = text.split(".", 1)
        high = int(high_text)
        low = int(low_text)
        if not (0 <= high <= 65_535 and 0 <= low <= 65_535):
            raise ValueError("invalid AS-Dot ASN")
        result = high * 65_536 + low
    else:
        result = int(text)
    if not 1 <= result <= 4_294_967_295:
        raise ValueError("ASN must be a positive 32-bit integer")
    return result


def _established(row: dict[str, object]) -> bool:
    explicit = row.get("established")
    if explicit is not None:
        return _true(explicit)
    state = _value(row, "state", "status")
    return state is not None and state.lower() == "established"


def _vrf(row: dict[str, object]) -> str:
    values = {
        value
        for value in (
            _value(row, "vrf"),
            _value(row, "routing-table", "routing_table"),
        )
        if value is not None
    }
    if len(values) != 1:
        raise ValueError("missing or conflicting exact VRF identity")
    return values.pop()


def _active_route(row: dict[str, object]) -> bool:
    if not _true(row.get("active")):
        return False
    return all(
        _false(row.get(field)) for field in ("disabled", "filtered", "unreachable")
    )


def _route_next_hop(row: dict[str, object]) -> tuple[str, str]:
    immediate = _value(row, "immediate-gw", "immediate_gateway")
    if immediate is None or "," in immediate:
        raise ValueError("missing or unsupported exact immediate gateway")
    next_hop, interface_name = _ip_and_zone(immediate)
    if interface_name is None:
        raise ValueError("immediate gateway does not identify an exact interface")
    return next_hop, interface_name


def _address_index(rows: list[dict[str, object]]) -> dict[str, set[str]]:
    index: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        if not _false(row.get("disabled")) or not _false(row.get("invalid")):
            continue
        raw_address = _value(row, "address")
        interface_name = _value(row, "interface")
        if raw_address is None or interface_name is None:
            continue
        try:
            address, _ = _ip_and_zone(raw_address)
        except ValueError:
            continue
        index[address].add(interface_name)
    return index


def _bgp_interface(row: dict[str, object], address_index: dict[str, set[str]]) -> str:
    local_address = _value(row, "local.address", "local-address", "local_address")
    local_ip, zone = _ip_and_zone(local_address)
    if zone is not None:
        return zone
    names = address_index.get(local_ip, set())
    if len(names) != 1:
        raise ValueError("local BGP address has no unique exact interface mapping")
    return next(iter(names))


def _evidence_sha256(
    *, endpoint: str, router_id: uuid.UUID, rows: list[dict[str, object]]
) -> str:
    payload = {
        "endpoint": endpoint,
        "router_id": str(router_id),
        "rows": rows,
        "schema_version": 1,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _client_ref(
    *,
    run_id: str,
    source_type: str,
    evidence_sha256: str,
    device_id: uuid.UUID,
    interface_id: uuid.UUID,
    vrf_name: str,
) -> uuid.UUID:
    return uuid.uuid5(
        COLLECTOR_NAMESPACE,
        ":".join(
            (
                run_id,
                source_type,
                evidence_sha256,
                str(device_id),
                str(interface_id),
                vrf_name,
            )
        ),
    )


def _exact_interface_index(
    interfaces: list[DeviceInterface],
) -> tuple[dict[str, DeviceInterface], set[str]]:
    grouped: dict[str, list[DeviceInterface]] = defaultdict(list)
    for interface in interfaces:
        grouped[interface.name].append(interface)
    ambiguous = {name for name, rows in grouped.items() if len(rows) != 1}
    return (
        {name: rows[0] for name, rows in grouped.items() if len(rows) == 1},
        ambiguous,
    )


def collect_forwarding_control_observations(
    db: Session,
    *,
    reader: ForwardingObservationReader | None = None,
    observed_at: datetime | None = None,
    ttl_seconds: int = DEFAULT_OBSERVATION_TTL_SECONDS,
    collector_run_id: str | None = None,
) -> dict[str, object]:
    """Collect declaration-scoped facts and submit them to the SOT owner."""

    reader = reader or RouterOSForwardingObservationReader()
    observed_at = observed_at or datetime.now(UTC)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must include a timezone")
    observed_at = observed_at.astimezone(UTC)
    ttl_seconds = max(int(ttl_seconds), MINIMUM_OBSERVATION_TTL_SECONDS)
    expires_at = observed_at + timedelta(seconds=ttl_seconds)
    run_id = collector_run_id or (f"{observed_at.isoformat()}:{uuid.uuid4()}")

    declarations = list(
        db.scalars(
            select(ForwardingTopologyDeclaration)
            .where(
                ForwardingTopologyDeclaration.active.is_(True),
                ForwardingTopologyDeclaration.path_kind.in_(
                    ("border_peer", "nas_termination")
                ),
            )
            .order_by(
                ForwardingTopologyDeclaration.downstream_device_id,
                ForwardingTopologyDeclaration.path_key,
            )
        ).all()
    )
    device_ids = {row.downstream_device_id for row in declarations}
    interfaces = list(
        db.scalars(
            select(DeviceInterface).where(DeviceInterface.device_id.in_(device_ids))
        ).all()
        if device_ids
        else []
    )
    routers = list(
        db.scalars(
            select(Router)
            .options(selectinload(Router.jump_host))
            .where(
                Router.is_active.is_(True),
                Router.network_device_id.in_(device_ids),
            )
            .order_by(Router.network_device_id, Router.id)
        ).all()
        if device_ids
        else []
    )

    declarations_by_device: dict[uuid.UUID, list[ForwardingTopologyDeclaration]] = (
        defaultdict(list)
    )
    interfaces_by_device: dict[uuid.UUID, list[DeviceInterface]] = defaultdict(list)
    routers_by_device: dict[uuid.UUID, list[Router]] = defaultdict(list)
    for declaration in declarations:
        declarations_by_device[declaration.downstream_device_id].append(declaration)
    for interface in interfaces:
        interfaces_by_device[interface.device_id].append(interface)
    for router in routers:
        if router.network_device_id is not None:
            routers_by_device[router.network_device_id].append(router)

    skipped: Counter[str] = Counter()
    failures: list[dict[str, str]] = []
    observation_ids: set[uuid.UUID] = set()
    bgp_submitted = 0
    routes_submitted = 0
    routers_polled = 0

    for device_id in sorted(device_ids, key=str):
        device_routers = routers_by_device.get(device_id, [])
        if len(device_routers) != 1:
            code = (
                "missing_router_binding"
                if not device_routers
                else "ambiguous_router_binding"
            )
            skipped[code] += 1
            failures.append({"code": code, "device_id": str(device_id)})
            continue
        router = device_routers[0]
        interface_index, ambiguous_names = _exact_interface_index(
            interfaces_by_device.get(device_id, [])
        )
        device_declarations = declarations_by_device[device_id]
        bgp_declarations = [
            row for row in device_declarations if row.path_kind == "border_peer"
        ]
        route_prefixes = sorted(
            {
                str(row.route_prefix)
                for row in device_declarations
                if row.route_prefix is not None
            }
        )
        try:
            if bgp_declarations:
                session_rows = reader.bgp_sessions(router)
                address_rows: list[dict[str, object]] = []
                families: set[int] = set()
                for row in session_rows:
                    local = _value(
                        row, "local.address", "local-address", "local_address"
                    )
                    if local is None:
                        continue
                    try:
                        local_ip, zone = _ip_and_zone(local)
                    except ValueError:
                        continue
                    if zone is None:
                        families.add(ipaddress.ip_address(local_ip).version)
                for family in sorted(families):
                    address_rows.extend(
                        reader.interface_addresses(router, family=family)
                    )
                address_index = _address_index(address_rows)
                expected_peer_ips = {
                    str(row.peer_ip)
                    for row in bgp_declarations
                    if row.peer_ip is not None
                }
                expected_peer_asns = {
                    int(row.peer_asn)
                    for row in bgp_declarations
                    if row.peer_asn is not None
                }
                for session_row in session_rows:
                    if not _established(session_row):
                        skipped["bgp_not_established"] += 1
                        continue
                    try:
                        peer_ip, _ = _ip_and_zone(
                            _value(
                                session_row,
                                "remote.address",
                                "remote-address",
                                "remote_address",
                            )
                        )
                        peer_asn = _asn(
                            _value(
                                session_row,
                                "remote.as",
                                "remote-as",
                                "remote_as",
                            )
                        )
                        if (
                            peer_ip not in expected_peer_ips
                            and peer_asn not in expected_peer_asns
                        ):
                            skipped["bgp_outside_declaration_scope"] += 1
                            continue
                        vrf_name = _vrf(session_row)
                        interface_name = _bgp_interface(session_row, address_index)
                        if interface_name in ambiguous_names:
                            raise ValueError("ambiguous exact interface inventory")
                        observed_interface = interface_index.get(interface_name)
                        if observed_interface is None:
                            raise ValueError(
                                "exact interface is missing from inventory"
                            )
                    except (TypeError, ValueError) as exc:
                        skipped["invalid_bgp_evidence"] += 1
                        failures.append(
                            {
                                "code": "invalid_bgp_evidence",
                                "device_id": str(device_id),
                                "error": str(exc),
                                "router_id": str(router.id),
                            }
                        )
                        continue
                    evidence_sha = _evidence_sha256(
                        endpoint="/routing/bgp/session",
                        router_id=router.id,
                        rows=[session_row, *address_rows],
                    )
                    observation = record_forwarding_control_observation(
                        db,
                        client_ref=_client_ref(
                            run_id=run_id,
                            source_type="bgp_peer",
                            evidence_sha256=evidence_sha,
                            device_id=device_id,
                            interface_id=observed_interface.id,
                            vrf_name=vrf_name,
                        ),
                        source_type="bgp_peer",
                        collector=COLLECTOR_NAME,
                        collector_run_id=run_id,
                        device_id=device_id,
                        interface_id=observed_interface.id,
                        vrf_name=vrf_name,
                        peer_ip=peer_ip,
                        peer_asn=peer_asn,
                        route_prefix=None,
                        next_hop_ip=None,
                        source_evidence_sha256=evidence_sha,
                        observed_at=observed_at,
                        expires_at=expires_at,
                        commit=False,
                    )
                    observation_ids.add(observation.id)
                    bgp_submitted += 1

            for route_prefix in route_prefixes:
                route_rows = reader.routes(router, prefix=route_prefix)
                for route_row in route_rows:
                    if not _active_route(route_row):
                        skipped["route_not_active"] += 1
                        continue
                    try:
                        observed_prefix = _prefix(
                            _value(route_row, "dst-address", "dst_address")
                        )
                        if observed_prefix != route_prefix:
                            raise ValueError("route row escaped exact prefix query")
                        vrf_name = _vrf(route_row)
                        next_hop_ip, interface_name = _route_next_hop(route_row)
                        if interface_name in ambiguous_names:
                            raise ValueError("ambiguous exact interface inventory")
                        observed_interface = interface_index.get(interface_name)
                        if observed_interface is None:
                            raise ValueError(
                                "exact interface is missing from inventory"
                            )
                    except (TypeError, ValueError) as exc:
                        skipped["invalid_route_evidence"] += 1
                        failures.append(
                            {
                                "code": "invalid_route_evidence",
                                "device_id": str(device_id),
                                "error": str(exc),
                                "router_id": str(router.id),
                            }
                        )
                        continue
                    evidence_sha = _evidence_sha256(
                        endpoint="/routing/route",
                        router_id=router.id,
                        rows=[route_row],
                    )
                    observation = record_forwarding_control_observation(
                        db,
                        client_ref=_client_ref(
                            run_id=run_id,
                            source_type="routing_table",
                            evidence_sha256=evidence_sha,
                            device_id=device_id,
                            interface_id=observed_interface.id,
                            vrf_name=vrf_name,
                        ),
                        source_type="routing_table",
                        collector=COLLECTOR_NAME,
                        collector_run_id=run_id,
                        device_id=device_id,
                        interface_id=observed_interface.id,
                        vrf_name=vrf_name,
                        peer_ip=None,
                        peer_asn=None,
                        route_prefix=observed_prefix,
                        next_hop_ip=next_hop_ip,
                        source_evidence_sha256=evidence_sha,
                        observed_at=observed_at,
                        expires_at=expires_at,
                        commit=False,
                    )
                    observation_ids.add(observation.id)
                    routes_submitted += 1
            routers_polled += 1
        except Exception as exc:  # noqa: BLE001 - isolate each router transport
            skipped["router_collection_failed"] += 1
            failures.append(
                {
                    "code": "router_collection_failed",
                    "device_id": str(device_id),
                    "error": str(exc),
                    "router_id": str(router.id),
                }
            )

    return {
        "bgp_observations_submitted": bgp_submitted,
        "collector": COLLECTOR_NAME,
        "collector_run_id": run_id,
        "declarations_in_scope": len(declarations),
        "expires_at": expires_at.isoformat(),
        "failures": failures,
        "observation_ids": sorted(str(value) for value in observation_ids),
        "observations_submitted": bgp_submitted + routes_submitted,
        "observed_at": observed_at.isoformat(),
        "route_observations_submitted": routes_submitted,
        "routers_polled": routers_polled,
        "skipped": dict(sorted(skipped.items())),
        "target_devices": len(device_ids),
    }


__all__ = [
    "COLLECTOR_NAME",
    "DEFAULT_OBSERVATION_TTL_SECONDS",
    "ForwardingObservationReader",
    "RouterOSForwardingObservationReader",
    "collect_forwarding_control_observations",
]
