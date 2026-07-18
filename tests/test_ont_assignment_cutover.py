from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime

from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.models.ont_assignment_identity import OntAssignmentIdentityDecision
from app.services.network.ont_assignment_cutover import audit_ont_assignment_cutover
from app.services.web_network_ont_identity_reviews import (
    list_assignment_identity_candidates,
)


def _assignment_plant(db_session, subscription, *, suffix: str):
    olt = OLTDevice(
        name=f"Cutover OLT {suffix}",
        hostname=f"cutover-{suffix}.example.test",
        is_active=True,
    )
    db_session.add(olt)
    db_session.flush()
    pon = PonPort(olt_id=olt.id, name="0/1/1", is_active=True)
    db_session.add(pon)
    db_session.flush()
    ont = OntUnit(
        serial_number=f"CUTOVER-{suffix}",
        olt_device_id=olt.id,
        pon_port_id=pon.id,
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(
        ont_unit_id=ont.id,
        pon_port_id=pon.id,
        subscriber_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        assigned_at=datetime.now(UTC),
        active=True,
    )
    db_session.add(assignment)
    db_session.commit()
    return olt, pon, ont, assignment


def test_exact_active_assignment_is_cutover_ready_and_audit_is_read_only(
    db_session, subscription
):
    _assignment_plant(db_session, subscription, suffix=uuid.uuid4().hex[:10])
    before_new = tuple(db_session.new)
    before_dirty = tuple(db_session.dirty)
    before_deleted = tuple(db_session.deleted)

    report = audit_ont_assignment_cutover(db_session)
    replay = audit_ont_assignment_cutover(db_session)

    assert report.ready_for_constraints is True
    assert report.active_assignment_count == 1
    assert report.clean_assignment_count == 1
    assert report.findings == ()
    assert all(gate.ready for gate in report.gates)
    assert replay.report_sha256 == report.report_sha256
    assert tuple(db_session.new) == before_new
    assert tuple(db_session.dirty) == before_dirty
    assert tuple(db_session.deleted) == before_deleted
    assert db_session.query(OntAssignmentIdentityDecision).count() == 0


def test_cutover_audit_reports_required_identity_inactive_targets_and_release_drift(
    db_session, subscription
):
    olt, pon, ont, assignment = _assignment_plant(
        db_session, subscription, suffix=uuid.uuid4().hex[:10]
    )
    olt.is_active = False
    pon.is_active = False
    ont.is_active = False
    assignment.subscription_id = None
    assignment.subscriber_id = None
    assignment.assigned_at = None
    assignment.released_at = datetime.now(UTC)
    assignment.release_reason = "legacy_release_marker"
    db_session.commit()

    report = audit_ont_assignment_cutover(db_session)
    finding = report.findings[0]

    assert report.ready_for_constraints is False
    assert finding.assignment_id == assignment.id
    assert {
        "active_release_marker",
        "inactive_assignment_pon",
        "inactive_assignment_pon_olt",
        "inactive_ont",
        "inactive_ont_olt",
        "inactive_ont_pon",
        "missing_assigned_at",
        "missing_subscriber",
        "missing_subscription",
    }.issubset(finding.reason_codes)
    assert finding.repair_owner == "network.ont_assignment_identity"
    assert finding.allowed_repair_actions == ("canonicalize", "deactivate")
    assert str(assignment.id) in finding.review_path
    assert len(finding.input_sha256) == 64
    assert db_session.query(OntAssignmentIdentityDecision).count() == 0


def test_duplicate_subscription_findings_bind_exact_related_assignments(
    db_session, subscription
):
    first = _assignment_plant(db_session, subscription, suffix=uuid.uuid4().hex[:10])[3]
    second = _assignment_plant(db_session, subscription, suffix=uuid.uuid4().hex[:10])[
        3
    ]

    report = audit_ont_assignment_cutover(db_session)
    by_id = {finding.assignment_id: finding for finding in report.findings}

    assert set(by_id) == {first.id, second.id}
    assert by_id[first.id].reason_codes == ("duplicate_active_subscription",)
    assert by_id[first.id].related_assignment_ids == (second.id,)
    assert by_id[second.id].related_assignment_ids == (first.id,)
    subscription_gate = next(
        gate
        for gate in report.gates
        if gate.name == "one_active_assignment_per_subscription"
    )
    assert subscription_gate.ready is False
    assert set(subscription_gate.blocking_assignment_ids) == {first.id, second.id}


def test_review_queue_filters_the_exhaustive_audit_before_applying_display_limit(
    db_session, subscription
):
    first = _assignment_plant(db_session, subscription, suffix=uuid.uuid4().hex[:10])[3]
    _second_olt, _second_pon, second_ont, second = _assignment_plant(
        db_session, subscription, suffix=uuid.uuid4().hex[:10]
    )
    report = audit_ont_assignment_cutover(db_session)

    candidates = list_assignment_identity_candidates(
        db_session,
        query=second_ont.serial_number,
        limit=1,
        cutover_audit=report,
    )

    assert len(candidates) == 1
    assert candidates[0].assignment_id == str(second.id)
    assert candidates[0].related_assignment_ids == (str(first.id),)
    assert candidates[0].repair_owner == "network.ont_assignment_identity"
    assert candidates[0].review_path.endswith(str(second.id))


def test_cutover_cli_exposes_no_apply_or_repair_mode(monkeypatch):
    from scripts.network import audit_ont_assignment_cutover

    monkeypatch.setattr(sys, "argv", ["audit_ont_assignment_cutover.py"])
    args = audit_ont_assignment_cutover.parse_args()

    assert args.compact is False
    assert not hasattr(args, "apply")
    assert not hasattr(args, "repair")
