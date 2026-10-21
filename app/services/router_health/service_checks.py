"""Service-path checks (layer 3): CoA outcomes, enforcement paths, canary results.

Pure functions over counts read by the reconciler; no I/O, no secrets.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.services.router_health.contracts import (
    CanaryDisposition,
    CheckId,
    HealthLayer,
    HealthPriority,
    PostureFinding,
)

_TIMEOUT_DISPOSITIONS = ("timeout", "transport_error")
_NOT_COUNTED = frozenset({"disabled"})
_CANARY_CONFIG = frozenset(
    {
        CanaryDisposition.missing_nas_host,
        CanaryDisposition.missing_dictionary,
        CanaryDisposition.invalid_dictionary,
        CanaryDisposition.missing_secret,
        CanaryDisposition.aborted_collision,
        CanaryDisposition.collision_check_unavailable,
    }
)
HEALTHY_CANARY = frozenset(
    {CanaryDisposition.rejected, CanaryDisposition.session_not_found}
)


@dataclass(frozen=True)
class EnforcementFailureRow:
    path: str
    failure_class: str
    count: int


def evaluate_coa_timeouts(
    nas_label: str,
    counts: Mapping[str, int],
    *,
    window_hours: int,
    min_count: int,
    ratio_pct: int,
) -> list[PostureFinding]:
    """``coa-timeouts``: enough timeouts, and a high enough share of attempts."""
    timeouts = sum(counts.get(name, 0) for name in _TIMEOUT_DISPOSITIONS)
    attempts = sum(v for k, v in counts.items() if k not in _NOT_COUNTED)
    if attempts <= 0 or timeouts < min_count:
        return []
    if timeouts * 100 < ratio_pct * attempts:
        return []
    return [
        PostureFinding(
            check=CheckId.coa_timeouts,
            qualifier="0",
            priority=HealthPriority.high,
            title=(
                f"{nas_label}: router is not answering CoA "
                f"({timeouts} timeouts in {window_hours} h)"
            ),
            impact=(
                "Live suspensions depend on the API/SSH fallback; when that "
                "fails customers stay online."
            ),
            evidence={
                **{f"count_{k}": int(v) for k, v in sorted(counts.items())},
                "timeouts": timeouts,
                "attempts": attempts,
                "window_hours": window_hours,
            },
            fix=(
                "Check /radius incoming accept, the shared secret and the 3799 "
                "path; on Gwarimpa upgrade RouterOS."
            ),
            layer=HealthLayer.service,
        )
    ]


def evaluate_enforcement_paths(
    nas_label: str,
    rows: Sequence[EnforcementFailureRow],
    *,
    min_count: int,
) -> list[PostureFinding]:
    findings: list[PostureFinding] = []
    for row in rows:
        if row.count < min_count:
            continue
        high = row.failure_class == "auth_rejected"
        findings.append(
            PostureFinding(
                check=CheckId.enforcement_path_failing,
                qualifier=f"{row.path}-{row.failure_class}".replace("_", "-"),
                priority=HealthPriority.high if high else HealthPriority.medium,
                title=(
                    f"{nas_label}: {row.path} enforcement failing "
                    f"({row.count} subscriptions, {row.failure_class})"
                ),
                impact=("Suspensions and restores may not take effect on this router."),
                evidence={
                    "path": row.path,
                    "failure_class": row.failure_class,
                    "subscriptions": row.count,
                },
                fix="Check the router API/SSH credentials and reachability.",
                layer=HealthLayer.service,
            )
        )
    return findings


def evaluate_canary_result(
    nas_label: str,
    disposition: CanaryDisposition,
    *,
    reprobe_failed: bool,
) -> list[PostureFinding]:
    """Findings for one final canary result (after any re-probe)."""
    if disposition in HEALTHY_CANARY:
        return []
    if disposition is CanaryDisposition.disconnected:
        return [
            PostureFinding(
                check=CheckId.coa_canary_acked,
                qualifier="0",
                priority=HealthPriority.critical,
                title=(
                    f"{nas_label}: router acknowledged a disconnect for a "
                    "non-existent user"
                ),
                impact=(
                    "CoA matching on this NAS is unsafe; investigate before "
                    "trusting enforcement."
                ),
                evidence={"disposition": disposition.value},
                fix="Do not rely on CoA for this NAS until the router is inspected.",
                layer=HealthLayer.service,
            )
        ]
    if disposition in {CanaryDisposition.timeout, CanaryDisposition.transport_error}:
        if not reprobe_failed:
            return []
        return [
            PostureFinding(
                check=CheckId.coa_canary_timeout,
                qualifier="0",
                priority=HealthPriority.high,
                title=f"{nas_label}: CoA canary got no reply",
                impact=(
                    "A suspension pushed by Sub would not take effect immediately."
                ),
                evidence={"disposition": disposition.value, "reprobed": True},
                fix=(
                    "Check /radius incoming accept, the secret and the 3799 path "
                    "from the sub-prod source address."
                ),
                layer=HealthLayer.service,
            )
        ]
    if disposition in _CANARY_CONFIG:
        reason = disposition.value.replace("_", " ")
        return [
            PostureFinding(
                check=CheckId.coa_canary_config,
                qualifier="0",
                priority=HealthPriority.medium,
                title=f"{nas_label}: canary could not be sent: {reason}",
                impact="CoA health cannot be verified for this NAS.",
                evidence={"disposition": disposition.value},
                fix="Fix the NAS host, shared secret or RADIUS dictionary setting.",
                layer=HealthLayer.service,
            )
        ]
    return [
        PostureFinding(
            check=CheckId.coa_canary_config,
            qualifier="0",
            priority=HealthPriority.medium,
            title=f"{nas_label}: canary result was not understood",
            impact="CoA health cannot be verified for this NAS.",
            evidence={"disposition": disposition.value},
            fix="Inspect the NAS RADIUS incoming configuration.",
            layer=HealthLayer.service,
        )
    ]


def evaluate_canary_stale(nas_label: str, *, interval_hours: int) -> PostureFinding:
    return PostureFinding(
        check=CheckId.coa_canary_stale,
        qualifier="0",
        priority=HealthPriority.low,
        title=f"{nas_label}: no CoA canary result recently",
        impact="CoA reachability has not been verified within twice the canary interval.",
        evidence={"interval_hours": interval_hours},
        fix="Check the canary task and its control.",
        layer=HealthLayer.service,
    )


FAILED_CANARY_VALUES = frozenset(
    {CanaryDisposition.timeout.value, CanaryDisposition.transport_error.value}
)


def evaluate_canary_unverified(
    nas_label: str, *, disposition: str, pending_minutes: int
) -> PostureFinding:
    """A first probe failed and its re-probe never reported back."""
    return PostureFinding(
        check=CheckId.coa_canary_unverified,
        qualifier="0",
        priority=HealthPriority.high,
        title=f"{nas_label}: CoA canary failed and was never re-verified",
        impact=(
            "The first canary probe got no reply and the follow-up probe did "
            "not complete; CoA reachability is unverified."
        ),
        evidence={"disposition": disposition, "pending_minutes": pending_minutes},
        fix="Check the Celery workers and queue, then the NAS CoA path.",
        layer=HealthLayer.service,
    )
