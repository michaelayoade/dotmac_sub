"""Alert escalation for reconcile failure modes.

The sweeper increments ``OntUnit.consecutive_sweep_unreachable`` each cycle
it cannot reach an ONT. After N consecutive cycles (default 3 ≈ 12h on a
4h sweep cadence) the operator needs to know — this module bridges the
in-process counter to the monitoring stack.

Single output path, best-effort: a **structured log line**, always emitted on
threshold crossings. Any log aggregator (Promtail, Splunk, etc.) can route on
``alert_kind=ont.sweep_unreachable`` plus the per-ONT metadata. (The Zabbix
trapper push that used to accompany it was retired with the native monitoring
cutover.)

Failure of the path does not propagate — the sweep cycle continues.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


# ── Sweep-unreachable alert escalation ─────────────────────────────────────


DEFAULT_SWEEP_THRESHOLD = 3
SWEEP_ALERT_KIND = "ont.sweep_unreachable"


def default_threshold_from_env() -> int:
    """``RECONCILE_SWEEP_ALERT_THRESHOLD`` env override, default 3."""
    raw = os.getenv("RECONCILE_SWEEP_ALERT_THRESHOLD", "").strip()
    if not raw:
        return DEFAULT_SWEEP_THRESHOLD
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "reconcile_sweep_alert_threshold_invalid",
            extra={"value": raw},
        )
        return DEFAULT_SWEEP_THRESHOLD
    return value if value > 0 else DEFAULT_SWEEP_THRESHOLD


def escalate_sweep_unreachable(
    *,
    ont_id: str,
    serial_number: str,
    mgmt_ip: str | None,
    before: int,
    after: int,
    threshold: int = DEFAULT_SWEEP_THRESHOLD,
) -> None:
    """Called by the sweeper after incrementing ``after``.

    Emits a structured ERROR log only on threshold crossings (``before <
    threshold <= after``) to avoid alert-fatigue per-sweep.

    Resolution is handled by ``resolve_sweep_unreachable``, called from
    the success path in core.py.
    """
    crossing = before < threshold <= after
    log_level = logging.ERROR if crossing else logging.DEBUG
    logger.log(
        log_level,
        SWEEP_ALERT_KIND,
        extra={
            "alert_kind": SWEEP_ALERT_KIND,
            "alert_action": "escalate" if crossing else "still_unreachable",
            "ont_id": str(ont_id),
            "serial_number": serial_number,
            "mgmt_ip": mgmt_ip,
            "before": before,
            "after": after,
            "threshold": threshold,
        },
    )


def resolve_sweep_unreachable(
    *,
    ont_id: str,
    serial_number: str,
    mgmt_ip: str | None,
    before: int,
) -> None:
    """Called from the reconcile core when the counter resets from a
    non-zero value to zero. Emits a single INFO ``resolved`` log."""
    if before <= 0:
        return

    logger.info(
        SWEEP_ALERT_KIND,
        extra={
            "alert_kind": SWEEP_ALERT_KIND,
            "alert_action": "resolved",
            "ont_id": str(ont_id),
            "serial_number": serial_number,
            "mgmt_ip": mgmt_ip,
            "before": before,
            "after": 0,
        },
    )


__all__ = (
    "DEFAULT_SWEEP_THRESHOLD",
    "default_threshold_from_env",
    "escalate_sweep_unreachable",
    "resolve_sweep_unreachable",
)
