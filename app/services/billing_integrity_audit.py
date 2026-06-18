"""Cross-domain integrity audit — billing × lifecycle × IPAM × RADIUS × NAS.

READ-ONLY. Proves that the independently-mutating domains agree with each other.
Each check returns ``{"count": int, "samples": [...]}``; the top-level
``audit_billing_integrity`` aggregates them and a metadata block. Designed to
back Prometheus gauges and a "billing automation must not launch unless these
are zero" gate. See docs/POST_CUTOVER_HARDENING.md.

Checks are added incrementally. This first cut covers the app-DB network /
lifecycle invariants and reuses ``ip_consistency_audit`` for IPv4 drift; the
billing-line invariants (disabled-service lines, add-on leakage, duplicate
period) are added once the billing-engine line semantics are mapped.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceLine
from app.models.catalog import (
    AccessCredential,
    AddOn,
    AddOnPrice,
    PriceType,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.network import IPAssignment, IPVersion
from app.services.ip_consistency_audit import (
    _external_ip_state,
    audit_ip_consistency,
)
from app.services.radius import _external_password_row

logger = logging.getLogger(__name__)

SAMPLE_LIMIT = 20

# Lifecycle status sets (mirrors radius_access_state, restated here so this audit
# reasons about them explicitly).
_TERMINAL = frozenset(
    {
        SubscriptionStatus.canceled,
        SubscriptionStatus.expired,
        SubscriptionStatus.disabled,
    }
)
# Any status that still implies the subscriber should hold network resources
# (serviceable or recoverable): active + blocked-family + not-yet-provisioned.
_NON_TERMINAL = frozenset(
    {
        SubscriptionStatus.active,
        SubscriptionStatus.blocked,
        SubscriptionStatus.suspended,
        SubscriptionStatus.stopped,
        SubscriptionStatus.pending,
        SubscriptionStatus.hidden,
        SubscriptionStatus.archived,
    }
)


def _result(samples: list[str]) -> dict[str, Any]:
    return {"count": len(samples), "samples": sorted(samples)[:SAMPLE_LIMIT]}


def _subscriber_ids_with_non_terminal_sub(db: Session) -> set[str]:
    return {
        str(sid)
        for (sid,) in db.execute(
            select(Subscription.subscriber_id)
            .where(Subscription.status.in_(_NON_TERMINAL))
            .distinct()
        ).all()
        if sid is not None
    }


def check_terminal_subscription_active_ip_assignment(db: Session) -> dict[str, Any]:
    """Subscribers holding an ACTIVE ipv4 IPAssignment while having NO
    non-terminal subscription — a terminal account still squatting an IP
    (release never ran / was asymmetric). Excludes reserved/management rows."""
    holders = {
        str(sid)
        for (sid,) in db.execute(
            select(IPAssignment.subscriber_id)
            .where(IPAssignment.is_active.is_(True))
            .where(IPAssignment.ip_version == IPVersion.ipv4)
            .distinct()
        ).all()
        if sid is not None
    }
    serviceable = _subscriber_ids_with_non_terminal_sub(db)
    return _result(list(holders - serviceable))


def check_duplicate_active_ipv4_assignment(db: Session) -> dict[str, Any]:
    """Subscribers with MORE THAN ONE active ipv4 IPAssignment — stale rows
    never released (the asymmetric-release fingerprint). The RADIUS projection
    becomes ambiguous about which IP is authoritative."""
    rows = db.execute(
        select(IPAssignment.subscriber_id, func.count())
        .where(IPAssignment.is_active.is_(True))
        .where(IPAssignment.ip_version == IPVersion.ipv4)
        .group_by(IPAssignment.subscriber_id)
        .having(func.count() > 1)
    ).all()
    return _result([str(sid) for sid, _ in rows if sid is not None])


def check_active_subscription_ip_drift(db: Session) -> dict[str, Any]:
    """Active subscriptions whose IPv4 disagrees across column / IPAM / radreply.
    Reuses ip_consistency_audit (external RADIUS read). Drift = every class
    except a pure dynamic sub."""
    ip = audit_ip_consistency(db)
    counts = ip.get("counts", {})
    total = sum(counts.values())
    # Surface a representative sample across the drift classes.
    samples: list[str] = []
    for kind in (
        "assignment_missing",
        "assignment_mismatch",
        "radreply_missing",
        "radreply_mismatch",
        "radreply_orphan",
    ):
        samples.extend(ip.get(kind, []))
    return {
        "count": total,
        "samples": samples[:SAMPLE_LIMIT],
        "by_class": counts,
        "errors": ip.get("errors", 0),
    }


# --- Billing-line invariants -------------------------------------------------

# A subscription is terminal (must not receive a recurring line for a period
# after it ended).
_TERMINAL_LINE_GUARD = _TERMINAL


def check_billing_disabled_service_lines(db: Session) -> dict[str, Any]:
    """Active invoice lines billing a TERMINAL subscription for a period that
    STARTS at/after the subscription ended (``canceled_at`` or ``end_at``).
    The recurring engine guards its create path, so any hit is from a manual
    invoice, a soft-delete edge, or a historical import — billed-for-dead."""
    ended_at = func.coalesce(Subscription.canceled_at, Subscription.end_at)
    rows = db.execute(
        select(Subscription.id)
        .join(InvoiceLine, InvoiceLine.subscription_id == Subscription.id)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(Subscription.status.in_(_TERMINAL_LINE_GUARD))
        .where(InvoiceLine.is_active.is_(True))
        .where(Invoice.is_active.is_(True))
        .where(ended_at.isnot(None))
        .where(Invoice.billing_period_start.isnot(None))
        .where(Invoice.billing_period_start > ended_at)
        .distinct()
    ).all()
    return _result([str(sid) for (sid,) in rows])


def check_billing_addon_without_billable_parent(db: Session) -> dict[str, Any]:
    """Live recurring add-ons (``end_at`` null/future, an active recurring
    ``AddOnPrice``) whose PARENT subscription is terminal — the classic
    'base service stops, add-on keeps billing' leak. ``_bill_recurring_addons``
    never re-checks the parent, so these would bill if the parent re-entered
    the run set."""
    now = datetime.now(UTC)
    recurring_addon = (
        select(AddOnPrice.add_on_id)
        .where(AddOnPrice.price_type == PriceType.recurring)
        .where(AddOnPrice.is_active.is_(True))
        .distinct()
        .subquery()
    )
    rows = db.execute(
        select(SubscriptionAddOn.subscription_id)
        .join(Subscription, Subscription.id == SubscriptionAddOn.subscription_id)
        .join(AddOn, AddOn.id == SubscriptionAddOn.add_on_id)
        .where(Subscription.status.in_(_TERMINAL))
        .where(
            or_(
                SubscriptionAddOn.end_at.is_(None),
                SubscriptionAddOn.end_at > now,
            )
        )
        .where(SubscriptionAddOn.add_on_id.in_(select(recurring_addon)))
        .distinct()
    ).all()
    return _result([str(sid) for (sid,) in rows if sid is not None])


def check_billing_duplicate_subscription_period_lines(db: Session) -> dict[str, Any]:
    """More than one active invoice line with the SAME (subscription, billing
    period, description). Keying on description avoids false-positiving the
    legitimate base-line + add-on-line that share a subscription and period."""
    rows = db.execute(
        select(
            InvoiceLine.subscription_id,
            Invoice.billing_period_start,
            Invoice.billing_period_end,
            InvoiceLine.description,
            func.count().label("n"),
        )
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(InvoiceLine.is_active.is_(True))
        .where(Invoice.is_active.is_(True))
        .where(InvoiceLine.subscription_id.isnot(None))
        .where(Invoice.billing_period_start.isnot(None))
        .group_by(
            InvoiceLine.subscription_id,
            Invoice.billing_period_start,
            Invoice.billing_period_end,
            InvoiceLine.description,
        )
        .having(func.count() > 1)
    ).all()
    return _result([str(r[0]) for r in rows])


def check_active_subscription_missing_radius(db: Session) -> dict[str, Any]:
    """Active subscriptions whose login is NOT present in external radcheck —
    the service can't authenticate. Uses the external RADIUS read."""
    logins = sorted(
        {
            login.strip()
            for (login,) in db.execute(
                select(Subscription.login)
                .where(Subscription.status == SubscriptionStatus.active)
                .where(Subscription.login.isnot(None))
            ).all()
            if login and login.strip()
        }
    )
    if not logins:
        return {"count": 0, "samples": [], "errors": 0}
    _framed, provisioned, errors = _external_ip_state(db, logins)
    missing = [login for login in logins if login not in provisioned]
    res = _result(missing)
    res["errors"] = errors
    return res


def check_active_subscription_with_unusable_radius_password(
    db: Session,
) -> dict[str, Any]:
    """Active subscriptions whose credential has NO usable RADIUS password
    (empty/unusable ``secret_hash``, or no/inactive credential). A radcheck row
    cannot be written without one, so these are the subset of
    ``active_subscription_missing_radius`` that re-sync CANNOT fix — they need a
    customer PPPoE password RESET, not provisioning. App-DB only (no external
    read): ``_external_password_row`` inspects the stored secret."""
    active = db.execute(
        select(Subscription.subscriber_id, Subscription.login)
        .where(Subscription.status == SubscriptionStatus.active)
        .where(Subscription.login.isnot(None))
    ).all()
    sub_ids = {sid for sid, _ in active if sid is not None}
    if not sub_ids:
        return {"count": 0, "samples": []}
    creds = db.scalars(
        select(AccessCredential).where(AccessCredential.subscriber_id.in_(sub_ids))
    ).all()
    cred_by = {(str(c.subscriber_id), c.username): c for c in creds}

    leak: list[str] = []
    for sid, login in active:
        login = (login or "").strip()
        if not login:
            continue
        cred = cred_by.get((str(sid), login))
        usable = (
            cred is not None
            and cred.is_active
            and _external_password_row(
                cred, default_attribute="Cleartext-Password", default_op=":="
            )
            is not None
        )
        if not usable:
            leak.append(login)
    return _result(leak)


# Registry of (name -> check fn).
_CHECKS = {
    # Billing-line invariants
    "billing_disabled_service_lines": check_billing_disabled_service_lines,
    "billing_duplicate_subscription_period_lines": (
        check_billing_duplicate_subscription_period_lines
    ),
    "billing_addon_without_billable_parent": (
        check_billing_addon_without_billable_parent
    ),
    # Network / lifecycle invariants
    "active_subscription_missing_radius": check_active_subscription_missing_radius,
    "active_subscription_with_unusable_radius_password": (
        check_active_subscription_with_unusable_radius_password
    ),
    "terminal_subscription_active_ip_assignment": (
        check_terminal_subscription_active_ip_assignment
    ),
    "duplicate_active_ipv4_assignment": check_duplicate_active_ipv4_assignment,
    "active_subscription_ip_drift": check_active_subscription_ip_drift,
}

# The first four gauges that HARD-BLOCK billing automation launch when non-zero.
_LAUNCH_BLOCKING = (
    "billing_disabled_service_lines",
    "billing_duplicate_subscription_period_lines",
    "billing_addon_without_billable_parent",
    "active_subscription_missing_radius",
)


def audit_billing_integrity(db: Session) -> dict[str, Any]:
    """Run every registered check. Returns
    ``{"checks": {name: {count, samples, ...}}, "counts": {name: int},
    "launch_blocked": bool, "errors": int}``."""
    checks: dict[str, Any] = {}
    errors = 0
    for name, fn in _CHECKS.items():
        try:
            checks[name] = fn(db)
        except Exception:
            logger.exception("billing_integrity_audit check failed: %s", name)
            checks[name] = {"count": 0, "samples": [], "error": True}
            errors += 1
    counts = {name: res.get("count", 0) for name, res in checks.items()}
    launch_blocked = any(counts.get(n, 0) for n in _LAUNCH_BLOCKING)
    return {
        "checks": checks,
        "counts": counts,
        "launch_blocked": launch_blocked,
        "errors": errors,
    }
