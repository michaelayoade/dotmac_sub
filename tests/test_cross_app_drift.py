"""Cross-app drift detector: framework lifecycle + the identity check.

Pins the durable behaviour the whole thing rests on — findings are created,
deduped by fingerprint across runs, resolved when they clear, suppressed while
waived, and keep/upgrade their severity — plus the first real check (CRM↔sub
duplicate identity).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.cross_app_drift import (
    EVENT_CREATED,
    EVENT_RECURRING,
    EVENT_RESOLVED,
    EVENT_WORSENED,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    STATUS_OPEN,
    STATUS_RESOLVED,
    STATUS_WAIVED,
    CrossAppDriftFinding,
    CrossAppDriftFindingEvent,
    CrossAppDriftWaiver,
)
from app.models.subscriber import Subscriber
from app.services import cross_app_drift
from app.services.cross_app_drift import Finding, run_detection


class _StubCheck:
    """A check that yields a fixed list of findings, for lifecycle tests."""

    name = "stub_check"

    def __init__(self, findings: list[Finding]):
        self._findings = findings

    def run(self, db):  # noqa: ANN001
        return list(self._findings)


def _finding(entity_id: str, severity: str = SEVERITY_HIGH) -> Finding:
    return Finding(
        check_name="stub_check",
        entity_type="thing",
        canonical_entity_id=entity_id,
        mismatch_type="mismatch",
        severity=severity,
        details={"entity_id": entity_id},
    )


def _events(db, finding_id) -> list[str]:
    return [
        e.event_type
        for e in db.query(CrossAppDriftFindingEvent)
        .filter_by(finding_id=finding_id)
        .all()
    ]


# --- framework lifecycle ---------------------------------------------------


def test_new_finding_created(db_session):
    run = run_detection(db_session, checks=[_StubCheck([_finding("a")])])

    findings = db_session.query(CrossAppDriftFinding).all()
    assert len(findings) == 1
    f = findings[0]
    assert f.status == STATUS_OPEN
    assert f.occurrences == 1
    assert run.findings_new == 1
    assert run.findings_open == 1
    assert _events(db_session, f.id) == [EVENT_CREATED]


def test_same_finding_deduped_by_fingerprint(db_session):
    check = _StubCheck([_finding("a")])
    run_detection(db_session, checks=[check])
    run2 = run_detection(db_session, checks=[check])

    findings = db_session.query(CrossAppDriftFinding).all()
    assert len(findings) == 1  # one row, not two
    f = findings[0]
    assert f.occurrences == 2
    assert run2.findings_new == 0
    assert _events(db_session, f.id) == [EVENT_CREATED, EVENT_RECURRING]


def test_resolved_finding_marked_resolved(db_session):
    run_detection(db_session, checks=[_StubCheck([_finding("a")])])
    # Next run no longer sees it -> resolved.
    run2 = run_detection(db_session, checks=[_StubCheck([])])

    f = db_session.query(CrossAppDriftFinding).one()
    assert f.status == STATUS_RESOLVED
    assert f.resolved_at is not None
    assert run2.findings_resolved == 1
    assert run2.findings_open == 0
    assert EVENT_RESOLVED in _events(db_session, f.id)


def test_waived_finding_suppressed(db_session):
    fp = _finding("a").fingerprint
    db_session.add(
        CrossAppDriftWaiver(
            fingerprint=fp,
            reason="known, tracked in JIRA-123",
            waived_by="michael",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=7),
            is_active=True,
        )
    )
    db_session.flush()

    run = run_detection(db_session, checks=[_StubCheck([_finding("a")])])

    f = db_session.query(CrossAppDriftFinding).one()
    assert f.status == STATUS_WAIVED
    # A waived finding is not counted as open (won't page).
    assert run.findings_open == 0
    assert cross_app_drift.open_findings_by_severity(db_session) == {}


def test_severity_preserved_and_worsens(db_session):
    # First seen at MEDIUM.
    run_detection(db_session, checks=[_StubCheck([_finding("a", SEVERITY_MEDIUM)])])
    f = db_session.query(CrossAppDriftFinding).one()
    assert f.severity == SEVERITY_MEDIUM

    # Same fingerprint (severity isn't part of it) but now HIGH -> worsened.
    run_detection(db_session, checks=[_StubCheck([_finding("a", SEVERITY_HIGH)])])
    db_session.refresh(f)
    assert f.severity == SEVERITY_HIGH
    assert EVENT_WORSENED in _events(db_session, f.id)


# --- the real identity check ----------------------------------------------


def _subscriber(db, crm_person_id: str) -> Subscriber:
    sub = Subscriber(
        first_name="Field",
        last_name="Tech",
        email=f"c-{uuid.uuid4().hex[:10]}@example.com",
        is_active=True,
        metadata_={"crm_person_id": crm_person_id},
    )
    db.add(sub)
    db.flush()
    return sub


def test_identity_check_flags_one_crm_person_with_two_subscribers(db_session):
    person = str(uuid.uuid4())
    a = _subscriber(db_session, person)
    b = _subscriber(db_session, person)
    # A different person with a single subscriber must NOT be flagged.
    _subscriber(db_session, str(uuid.uuid4()))
    db_session.flush()

    run_detection(db_session)

    findings = (
        db_session.query(CrossAppDriftFinding)
        .filter_by(check_name="identity_cardinality")
        .all()
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == SEVERITY_HIGH
    assert f.mismatch_type == "duplicate_sub_subscriber"
    assert f.canonical_entity_id == person
    assert set(f.evidence["sub_subscriber_ids"]) == {str(a.id), str(b.id)}
    assert f.details["suggested_owner"]


# --- service enforcement check ---------------------------------------------


def _subscription(db, subscriber, offer, status, *, updated_at=None):
    from app.models.catalog import Subscription

    sub = Subscription(subscriber_id=subscriber.id, offer_id=offer.id, status=status)
    db.add(sub)
    db.flush()
    if updated_at is not None:
        sub.updated_at = updated_at
        db.flush()
    return sub


def _live_session(db, subscriber, subscription, *, now):
    from app.models.radius_active_session import RadiusActiveSession

    db.add(
        RadiusActiveSession(
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            username=f"u-{uuid.uuid4().hex[:8]}",
            acct_session_id=uuid.uuid4().hex,
            session_start=now,
            last_update=now,
        )
    )
    db.flush()


def test_suspended_but_online_is_critical(db_session, subscriber, catalog_offer):
    from app.models.catalog import SubscriptionStatus

    now = datetime.now(UTC)
    sub = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        SubscriptionStatus.suspended,
        updated_at=now - timedelta(hours=1),  # suspended long enough (past grace)
    )
    _live_session(db_session, subscriber, sub, now=now)

    run_detection(db_session, checks=[cross_app_drift.ServiceEnforcementCheck()])

    f = (
        db_session.query(CrossAppDriftFinding)
        .filter_by(mismatch_type="suspended_but_online")
        .one()
    )
    assert f.severity == "critical"
    assert f.canonical_entity_id == str(subscriber.id)
    assert f.evidence["active_sessions"] == 1
    assert f.evidence["radius_authorized"] is True
    assert "suspended" in f.evidence["billing_status"]


def test_recently_suspended_is_within_grace(db_session, subscriber, catalog_offer):
    from app.models.catalog import SubscriptionStatus

    now = datetime.now(UTC)
    sub = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        SubscriptionStatus.suspended,
        updated_at=now,  # just suspended — enforcement may still be in flight
    )
    _live_session(db_session, subscriber, sub, now=now)

    run_detection(db_session, checks=[cross_app_drift.ServiceEnforcementCheck()])

    assert (
        db_session.query(CrossAppDriftFinding)
        .filter_by(mismatch_type="suspended_but_online")
        .count()
        == 0
    )


def test_active_and_online_is_not_flagged(db_session, subscriber, catalog_offer):
    from app.models.catalog import SubscriptionStatus

    now = datetime.now(UTC)
    sub = _subscription(
        db_session, subscriber, catalog_offer, SubscriptionStatus.active
    )
    _live_session(db_session, subscriber, sub, now=now)

    run_detection(db_session, checks=[cross_app_drift.ServiceEnforcementCheck()])

    assert (
        db_session.query(CrossAppDriftFinding)
        .filter_by(check_name="service_enforcement")
        .count()
        == 0
    )


def test_active_but_blocked_is_high(db_session, subscriber, catalog_offer):
    from app.models.catalog import SubscriptionStatus
    from app.models.subscriber import SubscriberStatus

    subscriber.status = SubscriberStatus.blocked  # walled-gardened at the BNG
    _subscription(db_session, subscriber, catalog_offer, SubscriptionStatus.active)
    db_session.flush()

    run_detection(db_session, checks=[cross_app_drift.ServiceEnforcementCheck()])

    f = (
        db_session.query(CrossAppDriftFinding)
        .filter_by(mismatch_type="active_but_blocked")
        .one()
    )
    assert f.severity == "high"
    assert f.canonical_entity_id == str(subscriber.id)
    assert f.evidence["subscriber_status"] == "blocked"


def test_throttle_profile_mismatch_is_medium(db_session, subscriber):
    from app.models.catalog import AccessCredential, RadiusProfile

    # A deactivated profile still referenced by a live credential — the FK
    # forbids a truly-missing one, so "inactive" is the realistic drift.
    dead_profile = RadiusProfile(name="retired-throttle", is_active=False)
    db_session.add(dead_profile)
    db_session.flush()
    db_session.add(
        AccessCredential(
            subscriber_id=subscriber.id,
            username=f"u-{uuid.uuid4().hex[:8]}",
            radius_profile_id=dead_profile.id,
            is_active=True,
        )
    )
    db_session.flush()

    run_detection(db_session, checks=[cross_app_drift.ServiceEnforcementCheck()])

    f = (
        db_session.query(CrossAppDriftFinding)
        .filter_by(mismatch_type="throttle_profile_mismatch")
        .one()
    )
    assert f.severity == "medium"
    assert f.evidence["radius_profile"] == "missing_or_inactive"
