"""GET-only RouterOS REST reader for the runtime poll (``network.router_health``).

Read-only in effect. Every request goes through ``RouterConnectionService
.execute_once`` (no database session per call) and must use a path from
``GET_PATHS``. The only POST is the optional optics monitor
(``OPTICS_PATH``) which RouterOS needs to report SFP diagnostics; it samples the
module once and changes nothing, and it runs only when the caller asks for it
(its own control, default off).

No usernames are requested from ``/ppp/active`` (``.proplist=.id`` and a count
only). Exception text is never kept: only a ``RestFailure`` class.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.models.router_management import Router
from app.services.router_health.contracts import (
    RestFailure,
    RuntimeObservation,
    RuntimeRow,
)
from app.services.router_management.connection import (
    RouterConnectionService,
    RouterResponse,
)

logger = logging.getLogger(__name__)

PATH_RESOURCE = "/system/resource?.proplist=uptime,version,board-name"
PATH_CLOCK = "/system/clock?.proplist=date,time,gmt-offset,time-zone-name"
PATH_NTP = "/system/ntp/client?.proplist=enabled,mode,servers,status"
PATH_BGP_CONNECTIONS = (
    "/routing/bgp/connection?.proplist=name,remote.address,disabled,local.role"
)
PATH_BGP_SESSIONS = "/routing/bgp/session?.proplist=name,remote.address,established"
PATH_OSPF = "/routing/ospf/neighbor?.proplist=router-id,address,state,interface"
# Minimal proplist; RouterOS REST also accepts ``&type=ether`` query filtering,
# but that is unproven on this estate's firmware, so ether rows are filtered
# client-side (see ``_ether_rows``) and the request stays unfiltered.
PATH_INTERFACES = "/interface?.proplist=name,type,running,disabled,link-downs"
PATH_PPP_ACTIVE = "/ppp/active?.proplist=.id"
PATH_USERS = "/user?.proplist=name,group,address,disabled"

#: The complete read allowlist; a test pins every request to this set.
GET_PATHS: frozenset[str] = frozenset(
    {
        PATH_RESOURCE,
        PATH_CLOCK,
        PATH_NTP,
        PATH_BGP_CONNECTIONS,
        PATH_BGP_SESSIONS,
        PATH_OSPF,
        PATH_INTERFACES,
        PATH_PPP_ACTIVE,
        PATH_USERS,
    }
)
OPTICS_PATH = "/interface/ethernet/monitor"
MAX_OPTICS_INTERFACES = 16
_SFP_NAME_RE = re.compile(r"^q?sfp", re.IGNORECASE)
_RX_FIELDS = ("sfp-rx-power", "rx-power", "sfp-rx-power-dbm")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

Transport = Callable[..., RouterResponse]


@dataclass(frozen=True)
class ReaderLimits:
    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    router_deadline: float = 45.0


_CREDENTIAL_FAILURES = frozenset({RestFailure.auth_failed, RestFailure.forbidden})


class _AccessFailure(Exception):
    """Internal: the router cannot be read (auth, forbidden, transport)."""

    def __init__(self, failure: RestFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


class _TransportAbort(Exception):
    """Internal: transport failed after the first success; keep data, stop polling."""


class _EndpointUnavailable(Exception):
    """Internal: this endpoint is unsupported or failed; carry its checks forward."""


def classify_exception(exc: BaseException) -> RestFailure | None:
    """Map a transport exception to an access failure; ``None`` = endpoint-local."""
    label = RouterConnectionService.classify_rest_failure(exc)
    return None if label is None else RestFailure(label)


def _text_row(row: Mapping[str, Any]) -> RuntimeRow:
    return {str(k): ("" if v is None else str(v)) for k, v in row.items()}


def _rows(payload: RouterResponse) -> tuple[RuntimeRow, ...]:
    if isinstance(payload, list):
        return tuple(_text_row(r) for r in payload if isinstance(r, dict))
    if isinstance(payload, dict) and payload:
        return (_text_row(payload),)
    return ()


def _ether_rows(rows: tuple[RuntimeRow, ...]) -> tuple[RuntimeRow, ...]:
    return tuple(r for r in rows if r.get("type", "").strip().lower() == "ether")


def sfp_interface_names(interfaces: tuple[RuntimeRow, ...]) -> list[str]:
    """Enabled ethernet interfaces named like SFP/QSFP cages (bounded).

    Only ``type=ether`` rows qualify, so a VLAN or bridge that merely carries an
    ``sfp`` name is never sent to the optics monitor.
    """
    names = [
        row["name"]
        for row in interfaces
        if row.get("name")
        and row.get("type", "").strip().lower() == "ether"
        and _SFP_NAME_RE.match(row["name"])
        and row.get("disabled", "").lower() != "true"
    ]
    return sorted(set(names))[:MAX_OPTICS_INTERFACES]


def parse_rx_dbm(row: Mapping[str, str]) -> float | None:
    for field in _RX_FIELDS:
        raw = row.get(field)
        if raw:
            match = _NUMBER_RE.search(raw)
            if match:
                return float(match.group())
    return None


class RouterOSRuntimeReader:
    """Poll one router's runtime state with fixed GET paths and bounded time."""

    def __init__(
        self,
        limits: ReaderLimits | None = None,
        *,
        transport: Transport | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits or ReaderLimits()
        self._transport: Transport = transport or RouterConnectionService.execute_once
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic

    def poll(
        self, router: Router, *, router_id: UUID, include_optics: bool
    ) -> RuntimeObservation:
        started = self._clock()
        deadline = self._monotonic() + self.limits.router_deadline
        collected: dict[str, Any] = {}
        server_time: datetime | None = None
        failure: RestFailure | None = None
        try:
            try:
                collected["resource"] = self._get_one(
                    router, PATH_RESOURCE, deadline, first=True
                )
                before = self._clock()
                collected["clock"] = self._get_optional_one(
                    router, PATH_CLOCK, deadline
                )
                after = self._clock()
                server_time = before + (after - before) / 2
                collected["ntp"] = self._get_optional_one(router, PATH_NTP, deadline)
                collected["bgp_connections"] = self._get_optional_rows(
                    router, PATH_BGP_CONNECTIONS, deadline
                )
                collected["bgp_sessions"] = self._get_optional_rows(
                    router, PATH_BGP_SESSIONS, deadline
                )
                collected["ospf_neighbors"] = self._get_optional_rows(
                    router, PATH_OSPF, deadline
                )
                interfaces = self._get_optional_rows(router, PATH_INTERFACES, deadline)
                if interfaces is not None:
                    interfaces = _ether_rows(interfaces)
                collected["interfaces"] = interfaces
                ppp = self._get_optional_rows(router, PATH_PPP_ACTIVE, deadline)
                collected["ppp_active_count"] = None if ppp is None else len(ppp)
                collected["users"] = self._get_optional_rows(
                    router, PATH_USERS, deadline
                )
                if include_optics and interfaces:
                    # Marked attempted before the POST so a failure still
                    # counts against the hourly bound.
                    collected["optics_attempted"] = True
                    collected["optics"] = self._read_optics(
                        router, sfp_interface_names(interfaces), deadline
                    )
            except _TransportAbort:
                # The router answered at least once: keep what was collected;
                # anything missing stays None (unjudged, findings carry forward).
                logger.info("router health runtime poll cut short by transport")
        except _AccessFailure as exc:
            failure = exc.failure
            collected = {}
            server_time = None
        except Exception:  # noqa: BLE001 - a poll never raises; class only is kept
            logger.warning("router health runtime poll failed unexpectedly")
            failure = RestFailure.other
            collected = {}
            server_time = None
        return RuntimeObservation(
            router_id=router_id,
            observed_at=started,
            failure=failure,
            server_time_utc=server_time,
            **collected,
        )

    # ------------------------------------------------------------------ helpers

    def _request(
        self,
        router: Router,
        method: str,
        path: str,
        deadline: float,
        *,
        payload: dict[str, str] | None = None,
        first: bool = False,
    ) -> RouterResponse:
        if method == "GET":
            if path not in GET_PATHS:
                raise ValueError("runtime poll path is not in the read allowlist")
        elif not (method == "POST" and path == OPTICS_PATH):
            raise ValueError("runtime poll only permits the optics monitor POST")
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            if first:
                raise _AccessFailure(RestFailure.unreachable)
            raise _EndpointUnavailable
        try:
            return self._transport(
                router,
                method=method,
                path=path,
                payload=payload,
                connect_timeout=min(self.limits.connect_timeout, remaining),
                read_timeout=min(self.limits.read_timeout, remaining),
            )
        except Exception as exc:  # noqa: BLE001 - classified, text discarded
            failure = classify_exception(exc)
            if failure is None:
                raise _EndpointUnavailable from None
            if first or failure in _CREDENTIAL_FAILURES:
                raise _AccessFailure(failure) from None
            # Transport trouble after the router already answered is not a
            # reachability verdict: stop here and keep the data collected.
            raise _TransportAbort from None

    def _get_one(
        self, router: Router, path: str, deadline: float, *, first: bool = False
    ) -> RuntimeRow:
        try:
            rows = _rows(self._request(router, "GET", path, deadline, first=first))
        except _EndpointUnavailable:
            if first:
                raise _AccessFailure(RestFailure.other) from None
            raise
        if not rows:
            raise _AccessFailure(RestFailure.other)
        return rows[0]

    def _get_optional_one(
        self, router: Router, path: str, deadline: float
    ) -> RuntimeRow | None:
        try:
            rows = _rows(self._request(router, "GET", path, deadline))
        except _EndpointUnavailable:
            return None
        return rows[0] if rows else None

    def _get_optional_rows(
        self, router: Router, path: str, deadline: float
    ) -> tuple[RuntimeRow, ...] | None:
        try:
            payload = self._request(router, "GET", path, deadline)
        except _EndpointUnavailable:
            return None
        return _rows(payload)

    def _read_optics(
        self, router: Router, names: list[str], deadline: float
    ) -> Mapping[str, float | None] | None:
        if not names:
            return {}
        try:
            payload = self._request(
                router,
                "POST",
                OPTICS_PATH,
                deadline,
                payload={"numbers": ",".join(names), "once": ""},
            )
        except (_EndpointUnavailable, _TransportAbort, _AccessFailure):
            # Optics is best effort: any failure (including a 403 on the POST)
            # leaves optics unjudged and never discards the GET data.
            return None
        readings: dict[str, float | None] = {}
        for row in _rows(payload):
            name = row.get("name")
            if name in names:
                readings[name] = parse_rx_dbm(row)
        return readings
