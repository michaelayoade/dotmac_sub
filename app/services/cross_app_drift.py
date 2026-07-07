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
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.cross_app_drift import (
    EVENT_CREATED,
    EVENT_RECURRING,
    EVENT_REOPENED,
    EVENT_RESOLVED,
    EVENT_WORSENED,
    RUN_COMPLETED,
    RUN_RUNNING,
    SEVERITY_HIGH,
    SEVERITY_ORDER,
    STATUS_OPEN,
    STATUS_RESOLVED,
    STATUS_WAIVED,
    CrossAppDriftFinding,
    CrossAppDriftFindingEvent,
    CrossAppDriftRun,
    CrossAppDriftWaiver,
)
from app.models.subscriber import Subscriber

logger = logging.getLogger(__name__)


@dataclass
class Finding:
    """One disagreement a check found this run."""

    check_name: str
    entity_type: str
    canonical_entity_id: str
    mismatch_type: str
    severity: str
    details: dict = field(default_factory=dict)

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
                details={
                    "crm_person_id": person_id,
                    "sub_subscriber_ids": sorted(sub_ids),
                    "count": len(sub_ids),
                    "suggested_owner": "sub CRM customer create-path (dedup)",
                    "suggested_action": (
                        "Merge the duplicate subscribers for this CRM person and "
                        "reconcile their invoices/payments; then add the unique key."
                    ),
                },
            )


DEFAULT_CHECKS: list[DriftCheck] = [IdentityCardinalityCheck()]


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
