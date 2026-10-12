"""Typed values for the router health reconciler (``network.router_health``)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

#: Evidence values are scalars only; no raw router lines or secrets.
EvidenceValue = str | int | float | bool | None | list[str] | list[int]

ALERT_MODE_SHADOW = "shadow"
ALERT_MODE_ALERT = "alert"
DETAILS_SCHEMA = "router_health.v1"
FINGERPRINT_ROOT = "network:router-health:"


class HealthPriority(StrEnum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


class HealthLayer(StrEnum):
    posture = "posture"
    snapshot = "snapshot"
    runtime = "runtime"
    service = "service"
    policy = "policy"


class SubjectKind(StrEnum):
    router = "router"
    nas = "nas"
    policy = "policy"


class CheckId(StrEnum):
    # snapshot layer
    snapshot_never = "snapshot-never"
    snapshot_stale = "snapshot-stale"
    snapshot_unparseable = "snapshot-unparseable"
    export_host_key_changed = "export-host-key-changed"
    # posture layer
    input_v4_missing = "input-v4-missing"
    input_v4_bypass = "input-v4-bypass"
    input_v6_missing = "input-v6-missing"
    snmp_exposed = "snmp-exposed"
    reverse_proxy_enabled = "reverse-proxy-enabled"
    ntp_client_off = "ntp-client-off"
    radius_incoming_off = "radius-incoming-off"
    suspension_jump = "suspension-jump"
    suspension_chain = "suspension-chain"
    suspension_shadowed = "suspension-shadowed"
    splynx_legacy_chain = "splynx-legacy-chain"
    splynx_legacy_list = "splynx-legacy-list"
    # service layer
    coa_timeouts = "coa-timeouts"
    enforcement_path_failing = "enforcement-path-failing"
    coa_canary_timeout = "coa-canary-timeout"
    coa_canary_acked = "coa-canary-acked"
    coa_canary_config = "coa-canary-config"
    coa_canary_stale = "coa-canary-stale"
    # policy layer
    policy_invalid = "policy-invalid"
    policy_unmatched = "policy-unmatched"
    policy_exception_expired = "policy-exception-expired"
    policy_overflow = "policy-overflow"


#: Checks whose policy name differs from the finding id (``checks_off`` names).
POSTURE_CHECKS_OFF_ALIASES: Mapping[str, tuple[CheckId, ...]] = {
    "suspension-jump": (
        CheckId.suspension_jump,
        CheckId.suspension_shadowed,
    ),
    "suspension-chain": (CheckId.suspension_chain,),
    "radius-incoming": (CheckId.radius_incoming_off,),
    "ppp-drop": (),
    "coa-canary": (
        CheckId.coa_canary_timeout,
        CheckId.coa_canary_acked,
        CheckId.coa_canary_config,
        CheckId.coa_canary_stale,
    ),
}


@dataclass(frozen=True)
class HealthFinding:
    layer: HealthLayer
    subject_kind: SubjectKind
    subject_id: UUID
    check: CheckId
    qualifier: str
    priority: HealthPriority
    title: str
    impact: str
    evidence: Mapping[str, EvidenceValue]
    fix: str
    target_url: str
    router_id: UUID | None = None
    nas_device_id: UUID | None = None
    policy_note: str | None = None
    known_issue: str | None = None
    snapshot_id: UUID | None = None
    snapshot_at: datetime | None = None


class ConfigExportFailureClass(StrEnum):
    auth_failed = "auth_failed"
    unreachable = "unreachable"
    host_key_changed = "host_key_changed"
    #: Strict (pinned-only) attempt against a host with no pinned key. Michael
    #: approved pre-seeded known_hosts instead of trust-on-first-use.
    host_key_unknown = "host_key_unknown"
    empty_export = "empty_export"
    no_auth_configured = "no_auth_configured"
    other = "other"


@dataclass(frozen=True)
class ConfigExportOutcome:
    router_id: UUID
    ok: bool
    failure_class: ConfigExportFailureClass | None
    at: datetime


class CoaKind(StrEnum):
    disconnect = "disconnect"
    update = "update"
    canary = "canary"


class CanaryDisposition(StrEnum):
    """Healthy: ``rejected`` / ``session_not_found`` (NAK). Critical: ACK."""

    disconnected = "disconnected"
    session_not_found = "session_not_found"
    rejected = "rejected"
    timeout = "timeout"
    transport_error = "transport_error"
    missing_nas_host = "missing_nas_host"
    missing_dictionary = "missing_dictionary"
    invalid_dictionary = "invalid_dictionary"
    missing_secret = "missing_secret"
    unexpected_response = "unexpected_response"
    aborted_collision = "aborted_collision"


@dataclass(frozen=True)
class CanaryTarget:
    host: str
    coa_port: int
    nas_device_ids: tuple[UUID, ...]
    primary_nas_device_id: UUID


@dataclass(frozen=True)
class CanaryResult:
    target: CanaryTarget
    disposition: CanaryDisposition
    reprobe: bool = False


@dataclass(frozen=True)
class PostureFinding:
    """A finding produced by a pure check, before a subject is attached."""

    check: CheckId
    qualifier: str
    priority: HealthPriority
    title: str
    impact: str
    evidence: Mapping[str, EvidenceValue]
    fix: str
    known_issue: str | None = None
    policy_note: str | None = None
    layer: HealthLayer = HealthLayer.posture


@dataclass(frozen=True)
class PostureResult:
    findings: tuple[PostureFinding, ...]
    #: Checks that were actually judged this run (pass or fail). Anything not
    #: listed is carried forward, never resolved.
    evaluated_checks: frozenset[CheckId]
    parse_coverage: float
