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

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import IPAssignment, IPv4Address, IPVersion
from app.services.common import coerce_uuid
from app.services.ip_consistency_audit import _external_ip_state, _norm

logger = logging.getLogger(__name__)

# Convergence is meaningful only for active subs: suspended subs have their
# radreply deleted by design, so "fixing" them would fight the enforcement
# path. Mirrors the audit population.
_CONVERGE_STATUS = SubscriptionStatus.active


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

    # Source of truth: IPAM if present, else the column.
    desired_ip = assign_ip or col_ip
    source = "ipam" if assign_ip else ("column" if col_ip else "none")

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
    for action in plan["actions"]:
        if action["kind"] == "set_column":
            subscription.ipv4_address = action["to"]
            applied.append("set_column")
            needs_refresh = True
        elif action["kind"] == "refresh_radius":
            needs_refresh = True
        # backfill_ipam: never auto-applied.

    if "set_column" in applied:
        db.commit()

    if needs_refresh:
        try:
            from app.tasks.splynx_sync import run_refresh_radius_from_subs

            run_refresh_radius_from_subs.delay()
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
