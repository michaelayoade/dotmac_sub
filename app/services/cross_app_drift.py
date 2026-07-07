"""Cross-app drift detector — runner, check registry, and checks.

Read-only. Each check answers one yes/no business rule and yields ``Finding``s;
the runner persists them by a stable fingerprint so the same drift is one row
tracked across runs (new / recurring / worsened / resolved / waived). It never
heals — every finding names the reconciler that should.

Scope note: dotmac_sub has no read path to ERP, so the first identity check
covers the CRM↔sub axis (the I-1 duplicate). The sub↔ERP leg and the money /
reseller / asset checks need an ERP read endpoint (or a dedicated ops service);
adding a check is just a new class in ``DEFAULT_CHECKS``.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.catalog import (
    AccessCredential,
    RadiusProfile,
    Subscription,
    SubscriptionStatus,
)
from app.models.cross_app_drift import (
    EVENT_CREATED,
    EVENT_RECURRING,
    EVENT_REOPENED,
    EVENT_RESOLVED,
    EVENT_WORSENED,
    RUN_COMPLETED,
    RUN_RUNNING,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    SEVERITY_ORDER,
    STATUS_OPEN,
    STATUS_RESOLVED,
    STATUS_WAIVED,
    CrossAppDriftFinding,
    CrossAppDriftFindingEvent,
    CrossAppDriftRun,
    CrossAppDriftWaiver,
)
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Subscriber

logger = logging.getLogger(__name__)

# A subscriber holding any of these is legitimately entitled to service.
_SERVICEABLE_STATUSES = {SubscriptionStatus.active, SubscriptionStatus.pending}
# ...and any of these means the subscriber should NOT be able to use service.
_NON_SERVICEABLE_STATUSES = {
    SubscriptionStatus.suspended,
    SubscriptionStatus.blocked,
    SubscriptionStatus.stopped,
    SubscriptionStatus.disabled,
    SubscriptionStatus.canceled,
    SubscriptionStatus.expired,
    SubscriptionStatus.archived,
}
# Grace so normal async enforcement lag (suspend -> CoA kick) isn't flagged.
_ENFORCEMENT_GRACE = timedelta(minutes=15)


@dataclass
class Finding:
    """One disagreement a check found this run."""

    check_name: str
    entity_type: str
    canonical_entity_id: str
    mismatch_type: str
    severity: str
    details: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        raw = "|".join(
            (
                self.check_name,
                self.entity_type,
                self.canonical_entity_id,
                self.mismatch_type,
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DriftCheck(Protocol):
    name: str

    def run(self, db: Session) -> Iterable[Finding]: ...


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


class IdentityCardinalityCheck:
    """CRM person ↔ sub subscriber cardinality (the axis sub can see today).

    Flags a CRM person mapped to more than one ACTIVE sub subscriber — the I-1
    duplicate that fragments billing / AR / the NCC subscriber count downstream.
    The advisory lock now prevents *new* ones; this proves none slipped and
    surfaces any that predate the lock. (The sub↔ERP leg needs an ERP read path.)
    """

    name = "identity_cardinality"

    def run(self, db: Session) -> Iterable[Finding]:
        rows = db.execute(
            select(
                Subscriber.metadata_["crm_person_id"].as_string(),
                Subscriber.id,
            ).where(Subscriber.is_active.is_(True))
        ).all()
        by_person: dict[str, list[str]] = {}
        for person_id, sub_id in rows:
            if not person_id:
                continue
            by_person.setdefault(str(person_id), []).append(str(sub_id))

        for person_id, sub_ids in by_person.items():
            if len(sub_ids) <= 1:
                continue
            yield Finding(
                check_name=self.name,
                entity_type="crm_person",
                canonical_entity_id=person_id,
                mismatch_type="duplicate_sub_subscriber",
                severity=SEVERITY_HIGH,
                evidence={
                    "crm_person_id": person_id,
                    "sub_subscriber_ids": sorted(sub_ids),
                    "sub_subscriber_count": len(sub_ids),
                },
                details={
                    "suggested_owner": "sub CRM customer create-path (dedup)",
                    "suggested_action": (
                        "Merge the duplicate subscribers for this CRM person and "
                        "reconcile their invoices/payments; then add the unique key."
                    ),
                },
            )


class ServiceEnforcementCheck:
    """Billing/subscription status ↔ RADIUS access state, self-contained in sub.

    Three distinct mismatch fingerprints so ownership + remediation stay obvious:

    * ``suspended_but_online`` (critical) — a subscriber with no serviceable
      subscription still has a live RADIUS session. Live unauthorized service /
      revenue leak; a grace window absorbs normal enforcement lag.
    * ``active_but_blocked`` (high) — a subscriber walled-gardened at the BNG
      (``status='blocked'``) while its subscriptions are all active. Paid but cut
      off — the account_status reconciler's cohort.
    * ``throttle_profile_mismatch`` (medium) — an active credential points at a
      missing/inactive RADIUS profile, so the intended profile (a throttle
      included) silently won't apply. Config drift, no immediate money impact.
    """

    name = "service_enforcement"

    def run(self, db: Session) -> Iterable[Finding]:
        now = datetime.now(UTC)
        yield from self._suspended_but_online(db, now)
        yield from self._active_but_blocked(db)
        yield from self._throttle_profile_mismatch(db)

    def _suspended_but_online(self, db: Session, now: datetime) -> Iterable[Finding]:
        grace_cutoff = now - _ENFORCEMENT_GRACE
        # Online subscribers (fresh RADIUS session) + their live-session counts.
        from app.services.topology.health_classifier import _fresh

        session_rows = db.execute(
            select(RadiusActiveSession.subscriber_id, func.count())
            .where(_fresh(now))
            .group_by(RadiusActiveSession.subscriber_id)
        ).all()
        session_counts = {sid: int(cnt) for sid, cnt in session_rows if sid}
        if not session_counts:
            return

        sub_rows = db.execute(
            select(
                Subscription.subscriber_id,
                Subscription.status,
                Subscription.updated_at,
            ).where(Subscription.subscriber_id.in_(list(session_counts)))
        ).all()
        by_subscriber: dict = defaultdict(list)
        for subscriber_id, status, updated_at in sub_rows:
            by_subscriber[subscriber_id].append((status, updated_at))

        for subscriber_id, items in by_subscriber.items():
            statuses = [status for status, _ in items]
            # Legitimately entitled if ANY subscription is serviceable.
            if any(status in _SERVICEABLE_STATUSES for status in statuses):
                continue
            if not any(status in _NON_SERVICEABLE_STATUSES for status in statuses):
                continue
            changed = [u for _, u in items if u is not None]
            latest_change = max(changed) if changed else None
            if latest_change is not None and latest_change.tzinfo is None:
                latest_change = latest_change.replace(tzinfo=UTC)  # SQLite naive
            # Grace: skip if the status changed within the enforcement window.
            if latest_change is not None and latest_change > grace_cutoff:
                continue
            yield Finding(
                check_name=self.name,
                entity_type="subscriber",
                canonical_entity_id=str(subscriber_id),
                mismatch_type="suspended_but_online",
                severity=SEVERITY_CRITICAL,
                evidence={
                    "billing_status": sorted({s.value for s in statuses}),
                    "radius_authorized": True,
                    "active_sessions": session_counts[subscriber_id],
                    "last_status_change": (
                        latest_change.isoformat() if latest_change else None
                    ),
                },
                details={
                    "suggested_owner": (
                        "enforcement reconciler "
                        "(app.tasks.radius.run_enforcement_reconciler)"
                    ),
                    "suggested_action": (
                        "CoA-kick the live session(s) and verify radcheck carries "
                        "Auth-Type := Reject for this subscriber."
                    ),
                },
            )

    def _active_but_blocked(self, db: Session) -> Iterable[Finding]:
        from app.services.account_status_reconcile import (
            find_blocked_all_active_account_ids,
        )

        for account_id in find_blocked_all_active_account_ids(db):
            yield Finding(
                check_name=self.name,
                entity_type="subscriber",
                canonical_entity_id=str(account_id),
                mismatch_type="active_but_blocked",
                severity=SEVERITY_HIGH,
                evidence={
                    "billing_status": "active",
                    "subscriber_status": "blocked",
                    "radius_authorized": False,
                    "note": "all subscriptions active but subscriber walled-gardened",
                },
                details={
                    "suggested_owner": (
                        "account-status reconciler "
                        "(app.tasks.enforcement.reconcile_account_status_drift)"
                    ),
                    "suggested_action": (
                        "Re-derive the subscriber status from its subscriptions "
                        "and refresh RADIUS so the walled-garden tag drops."
                    ),
                },
            )

    def _throttle_profile_mismatch(self, db: Session) -> Iterable[Finding]:
        active_profile_ids = set(
            db.scalars(
                select(RadiusProfile.id).where(RadiusProfile.is_active.is_(True))
            ).all()
        )
        rows = db.execute(
            select(
                AccessCredential.subscriber_id,
                AccessCredential.username,
                AccessCredential.radius_profile_id,
            ).where(
                AccessCredential.is_active.is_(True),
                AccessCredential.radius_profile_id.isnot(None),
            )
        ).all()
        for subscriber_id, username, profile_id in rows:
            if profile_id in active_profile_ids:
                continue
            yield Finding(
                check_name=self.name,
                entity_type="subscriber",
                canonical_entity_id=str(subscriber_id),
                mismatch_type="throttle_profile_mismatch",
                severity=SEVERITY_MEDIUM,
                evidence={
                    "credential_username": username,
                    "radius_profile_id": str(profile_id),
                    "radius_profile": "missing_or_inactive",
                    "expected_profile": "an active RadiusProfile",
                },
                details={
                    "suggested_owner": "billing / enforcement config",
                    "suggested_action": (
                        "Point the credential at an active RadiusProfile (or clear "
                        "the stale reference); as-is any intended profile — a "
                        "throttle included — silently won't apply."
                    ),
                },
            )


DEFAULT_CHECKS: list[DriftCheck] = [
    IdentityCardinalityCheck(),
    ServiceEnforcementCheck(),
]


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


def _active_waiver_fingerprints(db: Session, now: datetime) -> set[str]:
    rows = db.scalars(
        select(CrossAppDriftWaiver.fingerprint).where(
            CrossAppDriftWaiver.is_active.is_(True),
            or_(
                CrossAppDriftWaiver.expires_at.is_(None),
                CrossAppDriftWaiver.expires_at > now,
            ),
        )
    ).all()
    return set(rows)


def _log_event(
    db: Session,
    finding: CrossAppDriftFinding,
    run: CrossAppDriftRun,
    event_type: str,
    snapshot: dict | None,
) -> None:
    db.add(
        CrossAppDriftFindingEvent(
            finding_id=finding.id,
            run_id=run.id,
            event_type=event_type,
            at=datetime.now(UTC),
            snapshot=snapshot,
        )
    )


def run_detection(
    db: Session, *, checks: list[DriftCheck] | None = None
) -> CrossAppDriftRun:
    """Execute every check, reconcile findings by fingerprint, persist, commit.

    Detect-only. Returns the completed ``CrossAppDriftRun`` with counts.
    """
    checks = checks if checks is not None else DEFAULT_CHECKS
    now = datetime.now(UTC)
    run = CrossAppDriftRun(status=RUN_RUNNING, started_at=now)
    db.add(run)
    db.flush()

    current: dict[str, Finding] = {}
    for check in checks:
        try:
            for produced in check.run(db):
                current[produced.fingerprint] = produced
        except Exception:
            logger.exception(
                "cross_app_drift check failed: %s", getattr(check, "name", check)
            )
    run.checks_run = len(checks)

    waived = _active_waiver_fingerprints(db, now)
    existing = {
        f.fingerprint: f for f in db.scalars(select(CrossAppDriftFinding)).all()
    }

    new_count = 0
    for fp, found in current.items():
        finding = existing.get(fp)
        if finding is None:
            finding = CrossAppDriftFinding(
                fingerprint=fp,
                check_name=found.check_name,
                entity_type=found.entity_type,
                canonical_entity_id=found.canonical_entity_id,
                mismatch_type=found.mismatch_type,
                severity=found.severity,
                status=STATUS_WAIVED if fp in waived else STATUS_OPEN,
                occurrences=1,
                first_seen_at=now,
                last_seen_at=now,
                first_run_id=run.id,
                last_run_id=run.id,
                details=found.details,
                evidence=found.evidence,
            )
            db.add(finding)
            db.flush()
            _log_event(db, finding, run, EVENT_CREATED, found.details)
            new_count += 1
            continue

        prev_status = finding.status
        prev_severity = finding.severity
        finding.occurrences += 1
        finding.last_seen_at = now
        finding.last_run_id = run.id
        finding.severity = found.severity
        finding.details = found.details
        finding.evidence = found.evidence
        finding.resolved_at = None

        if fp in waived:
            finding.status = STATUS_WAIVED
        elif prev_status in (STATUS_RESOLVED, STATUS_WAIVED):
            finding.status = STATUS_OPEN
            _log_event(db, finding, run, EVENT_REOPENED, found.details)

        if SEVERITY_ORDER.get(found.severity, 0) > SEVERITY_ORDER.get(prev_severity, 0):
            _log_event(db, finding, run, EVENT_WORSENED, found.details)
        else:
            _log_event(db, finding, run, EVENT_RECURRING, found.details)

    resolved_count = 0
    for fp, finding in existing.items():
        if fp in current:
            continue
        if finding.status in (STATUS_OPEN, STATUS_WAIVED):
            finding.status = STATUS_RESOLVED
            finding.resolved_at = now
            finding.last_run_id = run.id
            _log_event(db, finding, run, EVENT_RESOLVED, None)
            resolved_count += 1

    db.flush()  # persist status changes so the open-count reflects this run
    open_count = db.scalar(
        select(func.count())
        .select_from(CrossAppDriftFinding)
        .where(CrossAppDriftFinding.status == STATUS_OPEN)
    )
    run.findings_open = int(open_count or 0)
    run.findings_new = new_count
    run.findings_resolved = resolved_count
    run.status = RUN_COMPLETED
    run.finished_at = datetime.now(UTC)
    db.commit()
    return run


def open_findings_by_severity(db: Session) -> dict[str, int]:
    """Current open-finding counts per severity — for alerting/metrics."""
    rows = db.execute(
        select(CrossAppDriftFinding.severity, func.count())
        .where(CrossAppDriftFinding.status == STATUS_OPEN)
        .group_by(CrossAppDriftFinding.severity)
    ).all()
    return {severity: int(count) for severity, count in rows}
