"""Connectivity reconciler — step 2 of the lifecycle hardening (IP dimension).

The strategic spine (see docs/designs/SERVICE_LIFECYCLE_BUNDLE_INTEGRITY.md):

> Status transitions set desired state only. One idempotent reconciler is the
> sole writer of connectivity state, run on every transition and on audit.

This module is the FIRST increment of that reconciler, scoped to the IPv4
dimension — the dominant risk (R2: ``subscription.ipv4_address`` is the only
copy of the address while suspended). It establishes the IPAM ``IPAssignment``
as the source of truth and converges the subscription column (and, via the
existing single-writer sweep, the external ``radreply`` Framed-IP) to it.

DEFAULTS TO SHADOW (``apply=False``): it computes and returns a plan but writes
nothing, matching the house pattern (radusergroup shadow path, prepaid-inert).
It is NOT yet wired into the live enforcement/provisioning handlers — that
cutover waits until the audit (``ip_consistency_audit``) quantifies real drift.
NAS-profile and address-list convergence are deliberately out of scope for this
increment; they remain with their current writers until the IP slice is proven.

Source-of-truth rule: an active IPAssignment (ipv4) wins; the column is a
cache. When only the column is set (``assignment_missing``), the converger
reports it but does NOT auto-fix — backfilling an IPAM row is a separate,
careful remediation, not a column rewrite.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import (
    AccessCredential,
    AccessState,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import IPAssignment, IPv4Address, IPVersion
from app.services.common import coerce_uuid
from app.services.ip_consistency_audit import _external_ip_state, _norm
from app.services.radius_access_state import derive_access_state

logger = logging.getLogger(__name__)

# Convergence is meaningful only for active subs: suspended subs have their
# radreply deleted by design, so "fixing" them would fight the enforcement
# path. Mirrors the audit population.
_CONVERGE_STATUS = SubscriptionStatus.active


# ---------------------------------------------------------------------------
# Step 2c — desired-state derivation + observability (shadow only, NO writes).
# See docs/designs/CONNECTIVITY_STATE_MACHINE.md §2 (transition table, INV-1..5)
# and §5 (guardrails). This increment proves the desired-state function and the
# shadow/legacy-write observability; it migrates no writers.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DesiredConnectivity:
    """Desired value of each connectivity dimension for ONE subscription,
    derived purely from its lifecycle status. The single place the §2
    transition table is encoded."""

    access_state: AccessState | None
    credentials_active: bool
    ip_active: bool
    ip_retained: bool
    kick_live_session: bool


def derive_desired_connectivity(
    status: SubscriptionStatus, *, hard_reject: bool = False
) -> DesiredConnectivity:
    """Pure: ``SubscriptionStatus`` (+ fraud ``hard_reject``) → desired
    connectivity. No I/O. Encodes the design-doc invariants:

    - pending/hidden/archived → not provisioned (no RADIUS row, no IP).
    - active → full access, IP active AND retained, don't kick.
    - blocked family (suspended/blocked/stopped) → ``captive`` by default,
      ``suspended`` under ``hard_reject``; credentials stay active and the IP is
      RETAINED (INV-1 paid→offline / INV-3 reversible), kick the live session
      once (INV-5).
    - terminal (canceled/expired/disabled) → ``terminated``; credentials
      inactive, IP released and cache cleared (INV-3/INV-4), kick once.
    """
    access = derive_access_state(status, hard_reject=hard_reject)
    if access is None:
        return DesiredConnectivity(None, False, False, False, False)
    if access is AccessState.active:
        return DesiredConnectivity(AccessState.active, True, True, True, False)
    if access in (AccessState.captive, AccessState.suspended):
        # Blocked but reversible: keep creds + IP, walled off at the RADIUS
        # group; disconnect any live session exactly once.
        return DesiredConnectivity(access, True, True, True, True)
    # AccessState.terminated
    return DesiredConnectivity(AccessState.terminated, False, False, False, True)


# Write-source marker. The reconciler is the legitimate single writer of
# connectivity-derived state. It wraps its apply phase in
# ``reconciler_write_scope()`` so ``note_connectivity_write`` — called from
# legacy writer sites as they are migrated — can distinguish a reconciler write
# (``source=reconciler``) from a legacy direct write and avoid false-positive
# noise once apply=True ships. Defaults to ``legacy`` so any unmarked caller is
# attributed correctly.
_write_source: contextvars.ContextVar[str] = contextvars.ContextVar(
    "connectivity_write_source", default="legacy"
)


@contextlib.contextmanager
def reconciler_write_scope() -> Iterator[None]:
    """Mark connectivity writes performed within as reconciler-originated."""
    token = _write_source.set("reconciler")
    try:
        yield
    finally:
        _write_source.reset(token)


def current_write_source() -> str:
    """The active connectivity write source (``reconciler`` or ``legacy``)."""
    return _write_source.get()


def note_connectivity_write(field: str, caller: str) -> None:
    """Record a write to a reconciler-owned connectivity field, attributed to
    the current write source. ``source=reconciler`` (inside
    ``reconciler_write_scope``) is the legitimate path; anything else is a legacy
    direct write to drive to zero before absorbing that writer. Low-noise (DEBUG
    for legacy), metric always, never raises."""
    source = _write_source.get()
    try:
        from app.metrics import CONNECTIVITY_DIRECT_WRITE

        CONNECTIVITY_DIRECT_WRITE.labels(field=field, source=source).inc()
    except Exception:  # pragma: no cover - metrics must never break a write
        pass
    if source != "reconciler":
        logger.debug(
            "legacy connectivity write field=%s caller=%s (not via reconciler)",
            field,
            caller,
        )


def connectivity_shadow_diff(db: Session, subscriber_id: Any) -> dict[str, Any]:
    """READ-ONLY. Compare desired connectivity (derived) vs actual stored state
    for a subscriber across access_state / credentials / IP. Logs and counts
    disagreements via ``connectivity_shadow_diff_total``. Writes nothing — the
    step-2c observability path that must look sane before any apply=True cutover.

    Returns a structured report; ``diffs`` lists the disagreeing dimensions.
    """
    sid = coerce_uuid(subscriber_id)
    subs = list(
        db.scalars(select(Subscription).where(Subscription.subscriber_id == sid)).all()
    )

    sub_reports: list[dict[str, Any]] = []
    any_creds_desired = False
    any_ip_desired = False
    access_mismatch = False
    ipv4_cache_mismatch = False
    for sub in subs:
        desired = derive_desired_connectivity(sub.status)
        any_creds_desired = any_creds_desired or desired.credentials_active
        any_ip_desired = any_ip_desired or desired.ip_active
        desired_access = (
            desired.access_state.value if desired.access_state is not None else None
        )
        match = desired_access == sub.access_state
        access_mismatch = access_mismatch or not match
        # ipv4_cache (INV-4 / R2): the served column is a PROJECTION of the
        # active assignment, not a second source of truth. When an IP is
        # retained (active/suspended), the column must equal the assignment IP;
        # divergence is the drift that the reconciler will own. Report-only —
        # this gauge sizes the cutover that removes the accounting dual-write.
        col_ip = _norm(sub.ipv4_address)
        assign_ip = _active_assignment_ip(db, sub)
        cache_match = (not desired.ip_retained) or (col_ip == assign_ip)
        ipv4_cache_mismatch = ipv4_cache_mismatch or not cache_match
        sub_reports.append(
            {
                "id": str(sub.id),
                "status": sub.status.value,
                "desired_access_state": desired_access,
                "actual_access_state": sub.access_state,
                "match": match,
                "served_ipv4": col_ip or None,
                "assignment_ipv4": assign_ip or None,
                "ipv4_cache_match": cache_match,
            }
        )

    actual_creds_active = (
        db.scalar(
            select(func.count())
            .select_from(AccessCredential)
            .where(
                AccessCredential.subscriber_id == sid,
                AccessCredential.is_active.is_(True),
            )
        )
        or 0
    )
    actual_ip_active = (
        db.scalar(
            select(func.count())
            .select_from(IPAssignment)
            .where(
                IPAssignment.subscriber_id == sid,
                IPAssignment.is_active.is_(True),
            )
        )
        or 0
    )

    creds_match = (actual_creds_active > 0) == any_creds_desired
    ip_match = (actual_ip_active > 0) == any_ip_desired

    diffs: list[str] = []
    if access_mismatch:
        diffs.append("access_state")
    if not creds_match:
        diffs.append("credentials_active")
    if not ip_match:
        diffs.append("ip_active")
    if ipv4_cache_mismatch:
        diffs.append("ipv4_cache")

    if diffs:
        try:
            from app.metrics import CONNECTIVITY_SHADOW_DIFF

            for dim in diffs:
                CONNECTIVITY_SHADOW_DIFF.labels(dimension=dim).inc()
        except Exception:  # pragma: no cover - observability must not raise
            pass
        logger.info(
            "connectivity shadow-diff subscriber=%s diffs=%s", subscriber_id, diffs
        )

    return {
        "subscriber_id": str(sid),
        "subscriptions": sub_reports,
        "credentials": {
            "desired": any_creds_desired,
            "actual": actual_creds_active > 0,
            "match": creds_match,
        },
        "ip": {
            "desired": any_ip_desired,
            "actual": actual_ip_active > 0,
            "match": ip_match,
        },
        "ipv4_cache": {
            "match": not ipv4_cache_mismatch,
        },
        "diffs": diffs,
    }


def _active_assignment_ip(db: Session, subscription: Subscription) -> str:
    """Address of the subscriber's active ipv4 IPAssignment. Empty if none.

    Keyed by subscriber, not subscription: IPAssignment is subscriber-scoped and
    prod's table has no ``subscription_id`` column (migration 153 stamped-not-
    applied — the alembic wedge in the design doc)."""
    if subscription.subscriber_id is None:
        return ""
    row = db.execute(
        select(IPv4Address.address)
        .join(IPAssignment, IPAssignment.ipv4_address_id == IPv4Address.id)
        .where(IPAssignment.is_active.is_(True))
        .where(IPAssignment.ip_version == IPVersion.ipv4)
        .where(IPAssignment.subscriber_id == subscription.subscriber_id)
    ).scalar()
    return _norm(row)


def plan_subscription_ip(
    db: Session, subscription: Subscription, *, trust_ipam: bool = False
) -> dict[str, Any]:
    """Pure-ish planner: compute desired IPv4 and the deltas vs the column and
    the external radreply Framed-IP. Reads only; returns a plan dict.

    ``trust_ipam`` (default False) decides whether a column-vs-IPAM mismatch
    becomes an applicable ``set_column`` or a report-only ``mismatch_adjudicate``
    — see the note inline; prod data (2026-06-17) makes IPAM the drifted side,
    so trusting it would change served IPs.

    Plan actions:
      - ``set_column``         — column ← IPAM (only when ``trust_ipam``).
      - ``mismatch_adjudicate``— column ≠ IPAM, report-only (default).
      - ``refresh_radius``     — external Framed-IP disagrees with desired; the
        single-writer sweep rebuilds radreply from the (corrected) column.
      - ``backfill_ipam``      — only the column is set (no IPAM row). REPORTED,
        not auto-applied (needs deliberate allocation).
    """
    col_ip = _norm(subscription.ipv4_address)
    assign_ip = _active_assignment_ip(db, subscription)
    login = (subscription.login or "").strip()

    # Source of truth: IPAM if present and trusted, else the served column.
    # Before IPAM has been repaired, a column/IPAM mismatch must not plan a
    # RADIUS refresh to the stale IPAM value.
    if assign_ip and (trust_ipam or not col_ip or assign_ip == col_ip):
        desired_ip = assign_ip
        source = "ipam"
    elif col_ip:
        desired_ip = col_ip
        source = "column"
    else:
        desired_ip = ""
        source = "none"

    radreply_ip = ""
    provisioned = False
    ext_errors = 0
    if login:
        framed_by_login, prov, ext_errors = _external_ip_state(db, [login])
        radreply_ip = framed_by_login.get(login, "")
        provisioned = login in prov

    actions: list[dict[str, str]] = []
    if assign_ip and col_ip and col_ip != assign_ip:
        # Direction matters and is DATA-DEPENDENT. The 2026-06-17 prod audit
        # showed radreply==column for every sub and IPAM as the drifted side
        # (323 mismatches), so rewriting the column from IPAM would change the
        # SERVED IP for live customers. `set_column` (column ← IPAM) is gated
        # behind ``trust_ipam`` and emitted as report-only otherwise.
        if trust_ipam:
            actions.append({"kind": "set_column", "from": col_ip, "to": assign_ip})
        else:
            actions.append(
                {
                    "kind": "mismatch_adjudicate",
                    "col": col_ip,
                    "ipam": assign_ip,
                    "note": "report-only; IPAM not yet trusted (see step 2a)",
                }
            )
    if col_ip and not assign_ip:
        actions.append({"kind": "backfill_ipam", "ip": col_ip, "note": "report-only"})
    if login and provisioned and desired_ip and _norm(radreply_ip) != desired_ip:
        actions.append(
            {"kind": "refresh_radius", "from": radreply_ip or "", "to": desired_ip}
        )

    return {
        "subscription_id": str(subscription.id),
        "login": login,
        "desired_ip": desired_ip,
        "source": source,
        "col_ip": col_ip,
        "assign_ip": assign_ip,
        "radreply_ip": _norm(radreply_ip),
        "actions": actions,
        "errors": ext_errors,
    }


def converge_subscription_connectivity(
    db: Session,
    subscription_id: str,
    *,
    apply: bool = False,
    trust_ipam: bool = False,
) -> dict[str, Any]:
    """Single idempotent entry point for IPv4 connectivity convergence.

    ``apply=False`` (default): SHADOW — compute and return the plan, write
    nothing. ``apply=True``: converge — enqueue the single-writer refresh if the
    external Framed-IP drifted, and (only when ``trust_ipam=True``) set the
    column from IPAM. ``backfill_ipam`` and ``mismatch_adjudicate`` are never
    auto-applied. With ``trust_ipam=False`` (default), ``apply=True`` can never
    change a served IP — given the 2026-06-17 prod data that would mass-rewrite
    live customers. Returns the plan augmented with ``applied`` /
    ``applied_actions``.
    """
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if subscription is None:
        return {"ok": False, "reason": "subscription_not_found", "actions": []}
    if subscription.status != _CONVERGE_STATUS:
        return {
            "ok": True,
            "reason": "not_active",
            "subscription_id": str(subscription_id),
            "actions": [],
            "applied": False,
        }

    plan = plan_subscription_ip(db, subscription, trust_ipam=trust_ipam)
    plan["ok"] = plan["errors"] == 0
    plan["applied"] = False
    plan["applied_actions"] = []

    if not apply:
        if plan["actions"]:
            logger.info(
                "connectivity reconciler (shadow) sub=%s would: %s",
                subscription_id,
                plan["actions"],
            )
        return plan

    applied: list[str] = []
    needs_refresh = False
    with reconciler_write_scope():
        for action in plan["actions"]:
            if action["kind"] == "set_column":
                subscription.ipv4_address = action["to"]
                note_connectivity_write(
                    "subscription.ipv4_address", "connectivity_reconciler"
                )
                applied.append("set_column")
                needs_refresh = True
            elif action["kind"] == "refresh_radius":
                needs_refresh = True
            # backfill_ipam: never auto-applied.

        if "set_column" in applied:
            db.commit()

    if needs_refresh:
        try:
            from app.tasks.radius_population import refresh_radius_from_subs

            refresh_radius_from_subs.delay()
            applied.append("refresh_radius")
        except Exception as exc:
            logger.error(
                "connectivity reconciler: failed to enqueue RADIUS refresh "
                "for sub=%s: %s (periodic sweep converges within 15 min)",
                subscription_id,
                exc,
            )

    plan["applied"] = bool(applied)
    plan["applied_actions"] = applied
    if applied:
        logger.info(
            "connectivity reconciler (apply) sub=%s did: %s",
            subscription_id,
            applied,
        )
    return plan
