"""Layer 2 runtime evaluation (pure): observation + prior signal state -> findings.

No I/O. ``evaluate_runtime`` takes one router's ``RuntimeObservation``, the prior
``router_health_signals`` state for it and the effective policy, and returns the
next signal states, the findings and the (check) scopes it fully judged. Checks
whose input was unavailable this poll are simply not judged, so their findings
carry forward. A router that cannot be read at all judges only the access checks.

Everything numeric is a bounded signal (ring buffer of at most ``SAMPLE_LIMIT``
samples); router text is never stored except short sanitised enum-like values.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from app.services.router_health.contracts import (
    CheckId,
    EvidenceValue,
    HealthFinding,
    HealthLayer,
    HealthPriority,
    RestFailure,
    RuntimeObservation,
    RuntimeRow,
    SignalState,
    SubjectKind,
)
from app.services.router_health.policy import EffectiveRouterPolicy
from app.services.router_health.runtime_reader import sfp_interface_names
from app.services.router_health.settings import RouterHealthSettings

SAMPLE_LIMIT = 200
SIGNAL_ACCESS = "rest_access"
SIGNAL_UPTIME = "uptime"
SIGNAL_NTP = "ntp"
SIGNAL_CLOCK = "clock"
SIGNAL_BGP = "bgp_peer"
SIGNAL_OSPF = "ospf_neighbor"
SIGNAL_LINK = "iface_link"
SIGNAL_OPTICS = "sfp_optics"
SIGNAL_OPTICS_ATTEMPT = "sfp_optics_attempt"
SIGNAL_PPP = "ppp_active"
SIGNAL_USERS = "users"
RUNTIME_SIGNALS = (
    SIGNAL_ACCESS,
    SIGNAL_UPTIME,
    SIGNAL_NTP,
    SIGNAL_CLOCK,
    SIGNAL_BGP,
    SIGNAL_OSPF,
    SIGNAL_LINK,
    SIGNAL_OPTICS,
    SIGNAL_OPTICS_ATTEMPT,
    SIGNAL_PPP,
    SIGNAL_USERS,
)
SCOPE_ROUTER = "router"
OSPF_BASELINE_FULL_SAMPLES = 4
OSPF_FORGET_AFTER = timedelta(days=7)
REBOOT_FINDING_WINDOW = timedelta(hours=24)
PPP_BASELINE_WINDOW = (timedelta(minutes=30), timedelta(minutes=150))
#: A PPP drop that has not recovered after this long is escalated (never cleared).
PPP_ESCALATE_AFTER = timedelta(hours=24)
SCOPE_MAX_LENGTH = 120
_SCOPE_KEEP = 96
PPP_MIN_ABSOLUTE_DROP = 10
PPP_CONSECUTIVE_POLLS = 2
OPTICS_MIN_INTERVAL = timedelta(minutes=55)
DARK_FIBRE_DBM = -40.0
MAX_NAMES_LISTED = 10

ACCESS_CHECKS = (
    CheckId.rest_auth_failed,
    CheckId.rest_forbidden,
    CheckId.rest_unreachable,
    CheckId.rest_tls,
)
OPTICS_CHECKS = (CheckId.sfp_rx_low, CheckId.sfp_overload)
_SAFE_TEXT_RE = re.compile(r"[^a-z0-9._ -]")


@dataclass(frozen=True)
class RuntimeRouter:
    router_id: UUID
    name: str


@dataclass
class RuntimeEvaluation:
    signals: list[SignalState] = field(default_factory=list)
    retire: list[tuple[str, str]] = field(default_factory=list)
    findings: list[HealthFinding] = field(default_factory=list)
    judged: set[CheckId] = field(default_factory=set)


# ------------------------------------------------------------------ small parsers


def safe_text(value: str | None, *, limit: int = 40) -> str:
    """Short enum-like text for evidence (lower-case, no punctuation surprises)."""
    return _SAFE_TEXT_RE.sub("", (value or "").lower())[:limit]


def bounded_scope(name: str) -> str:
    """A signal scope that always fits ``String(120)``.

    Short names pass through unchanged; long ones keep a prefix and a short hash
    of the full name so distinct interfaces never collide.
    """
    if len(name) <= SCOPE_MAX_LENGTH:
        return name
    digest = hashlib.sha1(name.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
    return f"{name[:_SCOPE_KEEP]}~{digest}"


def display_name(name: str, limit: int = 60) -> str:
    """Interface name for titles and evidence (bounded; full text is not kept)."""
    return name if len(name) <= limit else f"{name[: limit - 1]}…"


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"true", "yes", "1"}


_UPTIME_PART_RE = re.compile(r"(\d+)([wdhms])")
_UPTIME_CLOCK_RE = re.compile(r"(\d+):(\d{2}):(\d{2})$")
_UPTIME_UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_uptime_seconds(value: str | None) -> int | None:
    """RouterOS uptime: ``1w2d3h4m5s`` and/or a trailing ``HH:MM:SS``."""
    if not value:
        return None
    text = value.strip()
    total = 0
    clock = _UPTIME_CLOCK_RE.search(text)
    if clock:
        total += int(clock[1]) * 3600 + int(clock[2]) * 60 + int(clock[3])
        text = text[: clock.start()]
    parts = _UPTIME_PART_RE.findall(text)
    if not parts and not clock:
        return None
    for amount, unit in parts:
        total += int(amount) * _UPTIME_UNITS[unit]
    return total


_OFFSET_RE = re.compile(r"^([+-])(\d{1,2}):?(\d{2})$")


def parse_gmt_offset_seconds(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    match = _OFFSET_RE.match(text)
    if match:
        seconds = int(match[2]) * 3600 + int(match[3]) * 60
        return -seconds if match[1] == "-" else seconds
    try:
        return int(text)
    except ValueError:
        return None


def _parse_clock_date(value: str) -> date | None:
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%b/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text.title() if "/" in text else text, fmt).date()
        except ValueError:
            continue
    return None


def router_utc(clock: RuntimeRow) -> datetime | None:
    """Router UTC from ISO or legacy ``mon/dd/yyyy`` dates and its GMT offset."""
    day = _parse_clock_date(clock.get("date", ""))
    if day is None:
        return None
    try:
        wall = datetime.strptime(clock.get("time", "").strip(), "%H:%M:%S").time()
    except ValueError:
        return None
    offset = parse_gmt_offset_seconds(clock.get("gmt-offset"))
    if offset is None:
        # No offset reported: trust the clock only when it declares UTC/GMT.
        if clock.get("time-zone-name", "").strip().upper() not in {"UTC", "GMT"}:
            return None
        offset = 0
    local = datetime.combine(day, wall, tzinfo=UTC)
    return local - timedelta(seconds=offset)


def normalize_peer(value: str | None) -> str | None:
    """Canonical host address of a BGP remote (strips ``/32``, ``%zone``, port)."""
    if not value:
        return None
    text = value.strip()
    if text.startswith("[") and "]" in text:
        text = text[1 : text.index("]")]
    text = text.split("%", 1)[0]
    if "/" in text:
        addr, _, prefix = text.partition("/")
        try:
            network = ipaddress.ip_network(f"{addr}/{prefix}", strict=False)
        except ValueError:
            return None
        if network.prefixlen != network.max_prefixlen:
            return None  # listen range / template, not a single peer
        text = addr
    elif text.count(":") == 1 and "." in text:
        text = text.split(":", 1)[0]  # "a.b.c.d:port"
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def _peer_qualifier(peer: str) -> str:
    return hashlib.sha1(peer.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def _address_covers(address_field: str, required: str) -> bool:
    """Does a RouterOS ``/user`` address list include ``required``? Empty = any."""
    if not address_field.strip():
        return True
    try:
        needed = ipaddress.ip_network(required, strict=False)
    except ValueError:
        return False
    for token in address_field.split(","):
        try:
            allowed = ipaddress.ip_network(token.strip(), strict=False)
        except ValueError:
            continue
        if allowed.version == needed.version and needed.subnet_of(allowed):  # type: ignore[arg-type]
            return True
    return False


# ------------------------------------------------------------------ signal helpers


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())


def _utc(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def advance_signal(
    prior: SignalState | None,
    *,
    signal: str,
    scope: str,
    abnormal: bool,
    value: float | None,
    text: str | None,
    now: datetime,
    append_sample: bool = True,
    boot_marker: str | None = None,
) -> SignalState:
    """Standard transition: ring buffer, abnormal streak and timestamps."""
    samples = list(prior.samples) if prior else []
    if append_sample and value is not None:
        samples.append((_epoch(now), float(value)))
    samples = samples[-SAMPLE_LIMIT:]
    if abnormal:
        streak = (prior.consecutive_abnormal if prior else 0) + 1
        first_abnormal = (
            _utc(prior.first_abnormal_at) if prior and prior.first_abnormal_at else now
        )
        last_normal = _utc(prior.last_normal_at) if prior else None
    else:
        streak = 0
        first_abnormal = None
        last_normal = now
    return SignalState(
        signal=signal,
        scope=scope,
        status="abnormal" if abnormal else "ok",
        last_value=value,
        last_text=text,
        samples=tuple(samples),
        first_abnormal_at=first_abnormal,
        last_normal_at=last_normal,
        last_observed_at=now,
        consecutive_abnormal=streak,
        boot_marker=boot_marker
        if boot_marker is not None
        else (prior.boot_marker if prior else None),
    )


def _minutes_since(moment: datetime | None, now: datetime) -> int:
    stamp = _utc(moment)
    if stamp is None:
        return 0
    return max(int((now - stamp).total_seconds() // 60), 0)


# ------------------------------------------------------------------ the evaluator


class _Builder:
    def __init__(
        self,
        router: RuntimeRouter,
        policy: EffectiveRouterPolicy,
        now: datetime,
    ) -> None:
        self.router = router
        self.policy = policy
        self.now = now
        self.out = RuntimeEvaluation()

    def add(
        self,
        check: CheckId,
        *,
        priority: HealthPriority,
        title: str,
        impact: str,
        evidence: Mapping[str, EvidenceValue],
        fix: str,
        qualifier: str = "0",
        policy_note: str | None = None,
    ) -> None:
        self.out.findings.append(
            HealthFinding(
                layer=HealthLayer.runtime,
                subject_kind=SubjectKind.router,
                subject_id=self.router.router_id,
                check=check,
                qualifier=qualifier,
                priority=priority,
                title=f"{self.router.name}: {title}",
                impact=impact,
                evidence=evidence,
                fix=fix,
                target_url=f"/admin/network/routers/{self.router.router_id}#health",
                router_id=self.router.router_id,
                policy_note=policy_note,
                known_issue=self.policy.known_issue(check.value),
            )
        )


def evaluate_runtime(
    *,
    router: RuntimeRouter,
    observation: RuntimeObservation,
    prior: Mapping[tuple[str, str], SignalState],
    policy: EffectiveRouterPolicy,
    settings: RouterHealthSettings,
    now: datetime,
    optics_enabled: bool,
) -> RuntimeEvaluation:
    """Judge one router's runtime poll (see module docstring)."""
    builder = _Builder(router, policy, now)
    _access(builder, observation, prior, settings)
    if observation.failure is not None:
        return builder.out
    reboot = _uptime(builder, observation, prior)
    _clock(builder, observation, prior, settings)
    _ntp(builder, observation, prior, settings)
    _bgp(builder, observation, prior, settings)
    _ospf(builder, observation, prior, settings)
    _links(builder, observation, prior, settings, reboot=reboot)
    _optics(builder, observation, prior, settings, optics_enabled=optics_enabled)
    _ppp(builder, observation, prior, settings)
    _users(builder, observation)
    return builder.out


def optics_due(prior: Mapping[tuple[str, str], SignalState], now: datetime) -> bool:
    """Optics are attempted at most hourly per router (``OPTICS_MIN_INTERVAL``).

    Gated on the router-level attempt marker, which is written on every attempt
    (reading, empty result or failure), not on per-interface readings.
    """
    marker = prior.get((SIGNAL_OPTICS_ATTEMPT, SCOPE_ROUTER))
    last = _utc(marker.last_observed_at) if marker else None
    return last is None or now - last >= OPTICS_MIN_INTERVAL


# -- access ----------------------------------------------------------------------


def _access(
    b: _Builder,
    obs: RuntimeObservation,
    prior: Mapping[tuple[str, str], SignalState],
    settings: RouterHealthSettings,
) -> None:
    b.out.judged.update(ACCESS_CHECKS)
    key = (SIGNAL_ACCESS, SCOPE_ROUTER)
    prev = prior.get(key)
    failure = obs.failure
    state = advance_signal(
        prev,
        signal=SIGNAL_ACCESS,
        scope=SCOPE_ROUTER,
        abnormal=failure is not None,
        value=0.0 if failure else 1.0,
        text=failure.value if failure else None,
        now=b.now,
    )
    b.out.signals.append(state)
    if failure is None:
        return
    user = b.policy.rest_user.name if b.policy.rest_user else "the REST user"
    evidence: dict[str, EvidenceValue] = {
        "failure_class": failure.value,
        "consecutive_polls": state.consecutive_abnormal,
        "failing_since_minutes": _minutes_since(state.first_abnormal_at, b.now),
    }
    if failure == RestFailure.auth_failed:
        b.add(
            CheckId.rest_auth_failed,
            priority=HealthPriority.high,
            title="Sub's router login is rejected",
            impact="Sub cannot read status or run API enforcement on this router.",
            evidence=evidence,
            fix=(
                f"Recreate {user} with the password Sub stores and an address "
                "that includes the sub-prod source."
            ),
        )
    elif failure == RestFailure.forbidden:
        b.add(
            CheckId.rest_forbidden,
            priority=HealthPriority.medium,
            title="Sub's router login lacks REST read rights",
            impact="Sub is authenticated but the router refuses its status reads.",
            evidence=evidence,
            fix=f"Give {user} the read, api and rest-api policies (group read).",
        )
    elif state.consecutive_abnormal >= settings.unreachable_polls:
        last_ppp = prior.get((SIGNAL_PPP, SCOPE_ROUTER))
        known_sessions = last_ppp is not None and (last_ppp.last_value or 0) > 0
        if failure == RestFailure.tls:
            b.add(
                CheckId.rest_tls,
                priority=HealthPriority.medium,
                title="router API TLS handshake fails",
                impact="Sub cannot read status or run API enforcement on this router.",
                evidence=evidence,
                fix="Check the router's API-SSL certificate and the TLS settings Sub uses.",
            )
        else:
            b.add(
                CheckId.rest_unreachable,
                priority=HealthPriority.high
                if known_sessions
                else HealthPriority.medium,
                title="router API unreachable from Sub",
                impact=(
                    "Sub cannot read status or run API enforcement; "
                    "customers were last seen on this router."
                    if known_sessions
                    else "Sub cannot read status or run API enforcement on this router."
                ),
                evidence={**evidence, "had_active_sessions": known_sessions},
                fix=(
                    "Check the management path, the REST service address ACL "
                    "and the router's power or uplink."
                ),
            )


# -- uptime / reboot -------------------------------------------------------------


def _uptime(
    b: _Builder,
    obs: RuntimeObservation,
    prior: Mapping[tuple[str, str], SignalState],
) -> bool:
    """Track uptime; return True when the router rebooted since the last poll."""
    if obs.resource is None:
        return False
    b.out.judged.add(CheckId.router_rebooted)
    seconds = parse_uptime_seconds(obs.resource.get("uptime"))
    prev = prior.get((SIGNAL_UPTIME, SCOPE_ROUTER))
    if seconds is None:
        return False
    rebooted = (
        prev is not None and prev.last_value is not None and seconds < prev.last_value
    )
    boot_marker = (b.now - timedelta(seconds=seconds)).strftime("%Y%m%d%H%M")
    abnormal = False
    reboot_at: datetime | None = None
    if rebooted:
        abnormal = True
        reboot_at = b.now
    elif prev is not None and prev.status == "abnormal" and prev.first_abnormal_at:
        started = _utc(prev.first_abnormal_at)
        if started is not None and b.now - started < REBOOT_FINDING_WINDOW:
            abnormal = True
            reboot_at = started
    state = advance_signal(
        prev if not rebooted else None,
        signal=SIGNAL_UPTIME,
        scope=SCOPE_ROUTER,
        abnormal=abnormal,
        value=float(seconds),
        text="rebooted" if abnormal else None,
        now=b.now,
        append_sample=False,
        boot_marker=boot_marker,
    )
    b.out.signals.append(state)
    if abnormal:
        b.add(
            CheckId.router_rebooted,
            priority=HealthPriority.low,
            title="router restarted",
            impact="Counters were reset and sessions on this router were dropped.",
            evidence={
                "uptime_seconds": seconds,
                "restarted_minutes_ago": _minutes_since(reboot_at, b.now),
            },
            fix="Confirm the restart was planned; check power and the system log.",
        )
    return rebooted


# -- clock / ntp -----------------------------------------------------------------


def _clock(
    b: _Builder,
    obs: RuntimeObservation,
    prior: Mapping[tuple[str, str], SignalState],
    settings: RouterHealthSettings,
) -> None:
    if obs.clock is None or obs.server_time_utc is None:
        return
    reported = router_utc(obs.clock)
    if reported is None:
        return
    b.out.judged.add(CheckId.clock_skew)
    skew = int((reported - obs.server_time_utc).total_seconds())
    over = abs(skew) > settings.clock_skew_seconds
    b.out.signals.append(
        advance_signal(
            prior.get((SIGNAL_CLOCK, SCOPE_ROUTER)),
            signal=SIGNAL_CLOCK,
            scope=SCOPE_ROUTER,
            abnormal=over,
            value=float(skew),
            text=None,
            now=b.now,
        )
    )
    if not over:
        return
    high = abs(skew) > settings.clock_skew_high_seconds
    b.add(
        CheckId.clock_skew,
        priority=HealthPriority.high if high else HealthPriority.medium,
        title=f"router clock is off by {_human_delta(abs(skew))}",
        impact="Logs, schedulers (including auto-rollback) and certificate checks drift.",
        evidence={
            "skew_seconds": skew,
            "threshold_seconds": settings.clock_skew_seconds,
            "ahead": skew > 0,
        },
        fix="Fix the NTP client or set the clock; then confirm it stays in sync.",
    )


def _human_delta(seconds: int) -> str:
    if seconds >= 86400:
        return f"{seconds // 86400} d"
    if seconds >= 3600:
        return f"{seconds // 3600} h"
    if seconds >= 60:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def _ntp(
    b: _Builder,
    obs: RuntimeObservation,
    prior: Mapping[tuple[str, str], SignalState],
    settings: RouterHealthSettings,
) -> None:
    if obs.ntp is None:
        return
    enabled = truthy(obs.ntp.get("enabled"))
    status = safe_text(obs.ntp.get("status")) or None
    if enabled and status is None:
        # An enabled client that reports no status cannot be judged either way
        # (missing field, older firmware): carry the finding forward, never
        # call it healthy.
        return
    b.out.judged.add(CheckId.ntp_unsynced)
    unsynced = (not enabled) or status != "synchronized"
    state = advance_signal(
        prior.get((SIGNAL_NTP, SCOPE_ROUTER)),
        signal=SIGNAL_NTP,
        scope=SCOPE_ROUTER,
        abnormal=unsynced,
        value=1.0 if enabled else 0.0,
        text=status,
        now=b.now,
        append_sample=False,
    )
    b.out.signals.append(state)
    minutes = _minutes_since(state.first_abnormal_at, b.now)
    if unsynced and minutes >= settings.ntp_grace_minutes:
        b.add(
            CheckId.ntp_unsynced,
            priority=HealthPriority.medium,
            title="router clock is not synchronised",
            impact="Logs, schedulers (including auto-rollback) and certificate checks drift.",
            evidence={
                "ntp_enabled": enabled,
                "ntp_status": status,
                "unsynchronised_minutes": minutes,
            },
            fix=(
                "Point the NTP client at a reachable server; if the client "
                "sends nothing, toggle the router's NTP server off and on."
            ),
        )


# -- bgp / ospf ------------------------------------------------------------------


def _bgp(
    b: _Builder,
    obs: RuntimeObservation,
    prior: Mapping[tuple[str, str], SignalState],
    settings: RouterHealthSettings,
) -> None:
    if obs.bgp_connections is None or obs.bgp_sessions is None:
        return
    b.out.judged.add(CheckId.bgp_session_down)
    expected: dict[str, str] = {}
    for row in obs.bgp_connections:
        if truthy(row.get("disabled")):
            continue
        peer = normalize_peer(row.get("remote.address"))
        if peer:
            expected[peer] = safe_text(row.get("name"))
    established = {
        peer
        for row in obs.bgp_sessions
        if truthy(row.get("established"))
        and (peer := normalize_peer(row.get("remote.address")))
    }
    for peer, name in sorted(expected.items()):
        down = peer not in established
        state = advance_signal(
            prior.get((SIGNAL_BGP, bounded_scope(peer))),
            signal=SIGNAL_BGP,
            scope=bounded_scope(peer),
            abnormal=down,
            value=0.0 if down else 1.0,
            text=name or None,
            now=b.now,
            append_sample=False,
        )
        b.out.signals.append(state)
        minutes = _minutes_since(state.first_abnormal_at, b.now)
        if not down or minutes < settings.bgp_grace_minutes:
            continue
        if b.policy.excepted(CheckId.bgp_session_down.value, peer):
            continue
        b.add(
            CheckId.bgp_session_down,
            priority=HealthPriority.high,
            # The peer address lives in the evidence (router:read), not the title,
            # which also reaches wider alert channels.
            title=f"BGP session {_peer_qualifier(peer)[:6]} is down",
            impact="Routing redundancy or customer transit is lost.",
            evidence={"peer": peer, "connection": name, "down_minutes": minutes},
            fix=(
                "Check the peer and the link. If the peer is retired, disable "
                "the connection or add a dated policy exception."
            ),
            qualifier=_peer_qualifier(peer),
        )
    expected_scopes = {bounded_scope(p) for p in expected}
    for signal, scope in prior:
        if signal == SIGNAL_BGP and scope not in expected_scopes:
            b.out.retire.append((signal, scope))


def _ospf(
    b: _Builder,
    obs: RuntimeObservation,
    prior: Mapping[tuple[str, str], SignalState],
    settings: RouterHealthSettings,
) -> None:
    if obs.ospf_neighbors is None:
        return
    b.out.judged.add(CheckId.ospf_neighbor_lost)
    seen: dict[str, str] = {}
    for row in obs.ospf_neighbors:
        rid = row.get("router-id", "").strip()
        if rid:
            seen[rid] = row.get("state", "").strip()
    seen = {bounded_scope(rid): text for rid, text in seen.items()}
    scopes = {scope for (signal, scope) in prior if signal == SIGNAL_OSPF} | set(seen)
    for rid in sorted(scopes):
        prev = prior.get((SIGNAL_OSPF, rid))
        state_text = seen.get(rid)
        full = state_text is not None and state_text.lower() == "full"
        full_count = int(prev.last_value or 0) if prev else 0
        if full:
            full_count = min(full_count + 1, 10_000)
        last_normal = _utc(prev.last_normal_at) if prev else None
        if (
            not full
            and prev is not None
            and last_normal is not None
            and b.now - last_normal > OSPF_FORGET_AFTER
        ):
            b.out.retire.append((SIGNAL_OSPF, rid))
            continue
        state = advance_signal(
            prev,
            signal=SIGNAL_OSPF,
            scope=rid,
            abnormal=not full,
            value=float(full_count),
            text=safe_text(state_text) or ("absent" if state_text is None else None),
            now=b.now,
            append_sample=False,
        )
        samples = list(prev.samples) if prev else []
        samples.append((_epoch(b.now), 1.0 if full else 0.0))
        state = SignalState(
            **{**state.__dict__, "samples": tuple(samples[-SAMPLE_LIMIT:])}
        )
        b.out.signals.append(state)
        minutes = _minutes_since(state.first_abnormal_at, b.now)
        if (
            not full
            and full_count >= OSPF_BASELINE_FULL_SAMPLES
            and minutes >= settings.ospf_grace_minutes
        ):
            b.add(
                CheckId.ospf_neighbor_lost,
                priority=HealthPriority.high,
                title=f"OSPF neighbour {rid} is no longer Full",
                impact="Internal routing redundancy through this neighbour is lost.",
                evidence={
                    "neighbor_router_id": rid,
                    "state": safe_text(state_text) or "absent",
                    "not_full_minutes": minutes,
                },
                fix=(
                    "Check the interface and the neighbour. A neighbour absent "
                    "for 7 days leaves the learned baseline and this resolves."
                ),
                qualifier=_peer_qualifier(rid),
            )


# -- interfaces -------------------------------------------------------------------


def _int(value: str | None) -> int | None:
    try:
        return int((value or "").strip())
    except ValueError:
        return None


def _links(
    b: _Builder,
    obs: RuntimeObservation,
    prior: Mapping[tuple[str, str], SignalState],
    settings: RouterHealthSettings,
    *,
    reboot: bool,
) -> None:
    if obs.interfaces is None:
        return
    b.out.judged.add(CheckId.link_flapping)
    window_start = _epoch(b.now - timedelta(hours=24))
    current: set[str] = set()
    for row in obs.interfaces:
        name = row.get("name", "").strip()
        counter = _int(row.get("link-downs"))
        if (
            not name
            or row.get("type", "").lower() != "ether"
            or truthy(row.get("disabled"))
            or counter is None
        ):
            continue
        scope = bounded_scope(name)
        current.add(scope)
        prev = prior.get((SIGNAL_LINK, scope))
        history = list(prev.samples) if prev else []
        if reboot or (history and counter < history[-1][1]):
            history = []  # counter reset: the baseline restarts here
        history.append((_epoch(b.now), float(counter)))
        history = history[-SAMPLE_LIMIT:]
        base = history[0]
        for sample in history:
            if sample[0] <= window_start:
                base = sample
            else:
                break
        delta = int(counter - base[1])
        flapping = delta >= settings.link_downs_24h_warn
        state = advance_signal(
            prev,
            signal=SIGNAL_LINK,
            scope=scope,
            abnormal=flapping,
            value=float(counter),
            text=None,
            now=b.now,
            append_sample=False,
        )
        b.out.signals.append(replace(state, samples=tuple(history)))
        if flapping:
            high = delta >= settings.link_downs_24h_high
            b.add(
                CheckId.link_flapping,
                priority=HealthPriority.high if high else HealthPriority.medium,
                title=f"{display_name(name)}: link went down {delta} times in 24 h",
                impact="A flapping link drops customer and transit traffic repeatedly.",
                evidence={
                    "interface": display_name(name),
                    "link_downs": delta,
                    "observed_hours": round((_epoch(b.now) - base[0]) / 3600, 1),
                },
                fix="Check the cable, optic and far-end port; replace the weakest part.",
                qualifier=scope.lower(),
            )
    for signal, scope in prior:
        if signal == SIGNAL_LINK and scope not in current:
            b.out.retire.append((signal, scope))


def _optics(
    b: _Builder,
    obs: RuntimeObservation,
    prior: Mapping[tuple[str, str], SignalState],
    settings: RouterHealthSettings,
    *,
    optics_enabled: bool,
) -> None:
    if not optics_enabled:
        # Control off: judge with no findings so open optics findings resolve.
        b.out.judged.update(OPTICS_CHECKS)
        return
    if obs.optics_attempted:
        # Router-level marker, written on every attempt so the hourly bound
        # holds even when the attempt failed or returned nothing.
        b.out.signals.append(
            advance_signal(
                prior.get((SIGNAL_OPTICS_ATTEMPT, SCOPE_ROUTER)),
                signal=SIGNAL_OPTICS_ATTEMPT,
                scope=SCOPE_ROUTER,
                abnormal=False,
                value=1.0 if obs.optics is not None else 0.0,
                text=None,
                now=b.now,
                append_sample=False,
            )
        )
    if obs.optics is None:
        return  # not polled, failed or unsupported: carry forward
    b.out.judged.update(OPTICS_CHECKS)
    running = {
        row.get("name", ""): truthy(row.get("running")) for row in obs.interfaces or ()
    }
    for name in sorted(
        set(obs.optics) & set(sfp_interface_names(obs.interfaces or ()))
    ):
        scope = bounded_scope(name)
        rx = obs.optics[name]
        if rx is None:
            continue
        priority: HealthPriority | None = None
        check = CheckId.sfp_rx_low
        note = "low"
        if rx > settings.sfp_overload_dbm:
            priority, check, note = (
                HealthPriority.high,
                CheckId.sfp_overload,
                "overload",
            )
        elif rx <= DARK_FIBRE_DBM and not running.get(name, False):
            priority, note = HealthPriority.high, "dark fibre"
        elif rx < settings.sfp_rx_critical_dbm:
            priority, note = HealthPriority.high, "critically low"
        elif rx < settings.sfp_rx_low_dbm:
            priority = HealthPriority.medium
        b.out.signals.append(
            advance_signal(
                prior.get((SIGNAL_OPTICS, scope)),
                signal=SIGNAL_OPTICS,
                scope=scope,
                abnormal=priority is not None,
                value=rx,
                text=safe_text(note) if priority else None,
                now=b.now,
            )
        )
        if priority is None:
            continue
        b.add(
            check,
            priority=priority,
            title=f"{display_name(name)}: optical receive level {rx:g} dBm ({note})",
            impact=(
                "The optical link is outside its safe range and may drop or damage the module."
                if check == CheckId.sfp_overload
                else "The optical link is marginal or dark and may drop traffic."
            ),
            evidence={
                "interface": display_name(name),
                "rx_dbm": rx,
                "low_dbm": settings.sfp_rx_low_dbm,
                "link_up": running.get(name, False),
            },
            fix=(
                "Add attenuation or check the transceiver type."
                if check == CheckId.sfp_overload
                else "Clean and reseat the fibre and optic; check the far end and the splice."
            ),
            qualifier=scope.lower(),
        )
    optic_scopes = {bounded_scope(n) for n in obs.optics}
    for signal, scope in prior:
        if signal == SIGNAL_OPTICS and scope not in optic_scopes:
            b.out.retire.append((signal, scope))


# -- ppp --------------------------------------------------------------------------


def _ppp(
    b: _Builder,
    obs: RuntimeObservation,
    prior: Mapping[tuple[str, str], SignalState],
    settings: RouterHealthSettings,
) -> None:
    if obs.ppp_active_count is None:
        return
    count = obs.ppp_active_count
    prev = prior.get((SIGNAL_PPP, SCOPE_ROUTER))
    samples = list(prev.samples) if prev else []
    # While a drop is open the baseline stays frozen at the samples taken before
    # it began: the window is anchored on the first abnormal poll, and abnormal
    # samples are never appended, so a sustained outage cannot renew the baseline.
    ongoing = (
        prev is not None
        and prev.status == "abnormal"
        and _utc(prev.first_abnormal_at) is not None
    )
    anchor = _utc(prev.first_abnormal_at) if ongoing and prev else None
    anchor = anchor or b.now
    lo = _epoch(anchor - PPP_BASELINE_WINDOW[1])
    hi = _epoch(anchor - PPP_BASELINE_WINDOW[0])
    window = [value for ts, value in samples if lo <= ts <= hi]
    baseline = statistics.median(window) if window else None
    dropped = (
        baseline is not None
        and baseline >= settings.ppp_min_baseline
        and count < baseline * (1 - settings.ppp_drop_pct / 100)
        and baseline - count >= PPP_MIN_ABSOLUTE_DROP
    )
    state = advance_signal(
        prev,
        signal=SIGNAL_PPP,
        scope=SCOPE_ROUTER,
        abnormal=bool(dropped),
        value=float(count),
        text=None,
        now=b.now,
        append_sample=not dropped,
    )
    b.out.signals.append(state)
    b.out.judged.add(CheckId.ppp_drop)
    if b.policy.check_off("ppp-drop"):
        return  # judged with no findings, so a role change resolves old ones
    if dropped and state.consecutive_abnormal >= PPP_CONSECUTIVE_POLLS:
        assert baseline is not None
        held = _minutes_since(state.first_abnormal_at, b.now)
        escalated = held >= PPP_ESCALATE_AFTER.total_seconds() // 60
        b.add(
            CheckId.ppp_drop,
            priority=HealthPriority.critical if escalated else HealthPriority.high,
            title=f"PPPoE sessions fell from {int(baseline)} to {count}"
            + (" (unrecovered for 24 h)" if escalated else ""),
            impact="A large share of customers on this router lost their session.",
            evidence={
                "baseline_sessions": int(baseline),
                "active_sessions": count,
                "drop_pct": int(round(100 * (baseline - count) / baseline)),
                "consecutive_polls": state.consecutive_abnormal,
                "dropped_minutes": held,
            },
            fix=(
                "Check the uplink, the PPPoE server and RADIUS reachability. "
                "The pre-drop baseline is held until sessions recover; this "
                "stays open (and escalates after 24 hours) until they do."
            ),
        )


# -- users ------------------------------------------------------------------------


def _users(b: _Builder, obs: RuntimeObservation) -> None:
    if obs.users is None:
        return
    users = {
        row.get("name", ""): row
        for row in obs.users
        if row.get("name") and not truthy(row.get("disabled"))
    }
    policy = b.policy
    if policy.ops_user is not None:
        b.out.judged.add(CheckId.ops_user)
        ops = policy.ops_user
        row = users.get(ops.name)
        if row is None:
            b.add(
                CheckId.ops_user,
                priority=HealthPriority.high,
                title=f"snapshot login {ops.name} is missing",
                impact="Sub cannot capture this router's configuration snapshots.",
                evidence={"user": ops.name, "problem": "missing"},
                fix=(
                    f"Create {ops.name} (group {ops.group}, the automation key) "
                    "with an address that includes the sub-prod source."
                ),
                qualifier="missing",
            )
        else:
            if not _address_covers(row.get("address", ""), ops.required_address):
                b.add(
                    CheckId.ops_user,
                    priority=HealthPriority.high,
                    title=f"snapshot login {ops.name} blocks the sub-prod address",
                    impact="Sub's snapshot logins are refused by the user's address list.",
                    evidence={"user": ops.name, "problem": "address"},
                    fix=f"Add the sub-prod source to {ops.name}'s allowed addresses.",
                    qualifier="address",
                )
            if row.get("group", "") != ops.group:
                b.add(
                    CheckId.ops_user,
                    priority=HealthPriority.medium,
                    title=f"snapshot login {ops.name} has group {safe_text(row.get('group'))}",
                    impact=(
                        f"The automation login has more rights than the {ops.group} "
                        "group it needs."
                    ),
                    evidence={
                        "user": ops.name,
                        "group": safe_text(row.get("group")),
                        "expected_group": ops.group,
                    },
                    fix=f"Move {ops.name} to group {ops.group}.",
                    qualifier="group",
                )
    if policy.rest_user is not None:
        b.out.judged.update({CheckId.rest_user_missing, CheckId.rest_user_full})
        rest = policy.rest_user
        row = users.get(rest.name)
        if row is None:
            b.add(
                CheckId.rest_user_missing,
                priority=HealthPriority.high,
                title=f"REST login {rest.name} is not in the router's user list",
                impact="Sub is reading this router with a different login than expected.",
                evidence={"user": rest.name},
                fix=f"Create {rest.name} with the rest-api read policy.",
            )
        elif row.get("group", "") in rest.forbidden_groups:
            b.add(
                CheckId.rest_user_full,
                priority=HealthPriority.medium,
                title=f"REST login {rest.name} has group {safe_text(row.get('group'))}",
                impact="Sub's API login can change the router; it only needs to read.",
                evidence={"user": rest.name, "group": safe_text(row.get("group"))},
                fix=f"Move {rest.name} to a least-privilege group.",
            )
    if policy.report_unrestricted_full_users:
        b.out.judged.add(CheckId.full_users_unrestricted)
        open_full = sorted(
            name
            for name, row in users.items()
            if row.get("group") == "full" and not row.get("address", "").strip()
        )
        if open_full:
            b.add(
                CheckId.full_users_unrestricted,
                priority=HealthPriority.low,
                title=f"{len(open_full)} full-rights users have no address limit",
                impact="Anyone who learns a password can administer the router from anywhere.",
                evidence={
                    "count": len(open_full),
                    "users": [
                        safe_text(n, limit=32) for n in open_full[:MAX_NAMES_LISTED]
                    ],
                },
                fix="Restrict each user's address list or move it to a lower group.",
            )


__all__ = [
    "RUNTIME_SIGNALS",
    "RuntimeEvaluation",
    "RuntimeRouter",
    "evaluate_runtime",
    "optics_due",
]
