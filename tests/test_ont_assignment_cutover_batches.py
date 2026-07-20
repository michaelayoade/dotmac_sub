from __future__ import annotations

import inspect
import sys
import uuid
from datetime import UTC, datetime

import pytest

from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.models.ont_assignment_cutover import (
    OntAssignmentCutoverBatchReview,
    OntAssignmentCutoverProposalBatch,
)
from app.models.ont_assignment_identity import OntAssignmentIdentityDecision
from app.services.network import ont_assignment_cutover_batches as batch_owner
from app.services.network.ont_assignment_cutover import audit_ont_assignment_cutover
from app.services.network.ont_assignment_cutover_batches import (
    OntAssignmentCutoverBatchBlocked,
    OntAssignmentCutoverBatchError,
    preview_ont_assignment_cutover_batch,
    propose_ont_assignment_cutover_batch,
    review_ont_assignment_cutover_batch,
)
from app.services.web_network_ont_identity_reviews import (
    list_cutover_proposal_batches,
)


def _drift_assignments(db_session, *, count: int = 1) -> list[OntAssignment]:
    suffix = uuid.uuid4().hex[:10]
    olt = OLTDevice(
        name=f"Batch OLT {suffix}",
        hostname=f"batch-{suffix}.example.test",
        is_active=True,
    )
    db_session.add(olt)
    db_session.flush()
    pon = PonPort(olt_id=olt.id, name="0/1/1", is_active=True)
    db_session.add(pon)
    db_session.flush()
    assignments: list[OntAssignment] = []
    for index in range(count):
        ont = OntUnit(
            serial_number=f"BATCH-{suffix}-{index}",
            olt_device_id=olt.id,
            pon_port_id=pon.id,
            is_active=True,
        )
        db_session.add(ont)
        db_session.flush()
        assignment = OntAssignment(
            ont_unit_id=ont.id,
            pon_port_id=pon.id,
            assigned_at=datetime.now(UTC),
            active=True,
        )
        db_session.add(assignment)
        assignments.append(assignment)
    db_session.commit()
    return assignments


def _deactivation_items(db_session) -> tuple[object, list[dict[str, object]]]:
    report = audit_ont_assignment_cutover(db_session)
    return report, [
        {
            "action": "deactivate",
            "assignment_id": str(finding.assignment_id),
            "duplicate_assignment_ids": [],
            "finding_sha256": finding.input_sha256,
            "reason": "Field verification confirmed this legacy assignment is stale",
        }
        for finding in report.findings
    ]


def _propose(db_session, report, items):
    return propose_ont_assignment_cutover_batch(
        db_session,
        expected_report_sha256=report.report_sha256,
        items=items,
        proposed_by="planner@example.com",
        reason="Reviewed assignment cleanup window",
        source_name="phase-12-test",
    )


def test_preview_rejects_stale_report_and_finding_without_writes(db_session):
    _drift_assignments(db_session)
    report, items = _deactivation_items(db_session)

    stale_report = preview_ont_assignment_cutover_batch(
        db_session,
        expected_report_sha256="0" * 64,
        items=items,
        proposed_by="planner@example.com",
        reason="Reviewed assignment cleanup window",
    )
    stale_finding_items = [{**items[0], "finding_sha256": "f" * 64}]
    stale_finding = preview_ont_assignment_cutover_batch(
        db_session,
        expected_report_sha256=report.report_sha256,
        items=stale_finding_items,
        proposed_by="planner@example.com",
        reason="Reviewed assignment cleanup window",
    )

    assert stale_report.ready is False
    assert "cutover_report_changed" in {
        blocker["code"] for blocker in stale_report.blockers
    }
    assert stale_finding.ready is False
    assert "finding_changed" in {blocker["code"] for blocker in stale_finding.blockers}
    assert db_session.query(OntAssignmentCutoverProposalBatch).count() == 0
    assert db_session.query(OntAssignmentIdentityDecision).count() == 0


def test_proposal_is_exact_atomic_and_idempotent(db_session):
    assignments = _drift_assignments(db_session, count=2)
    report, items = _deactivation_items(db_session)

    result = _propose(db_session, report, items)
    replay = _propose(db_session, report, items)

    assert result.created is True
    assert replay.created is False
    assert replay.batch.id == result.batch.id
    assert result.batch.report_sha256 == report.report_sha256
    assert result.batch.item_count == 2
    assert len(result.batch.manifest_sha256) == 64
    assert [decision.proposal_batch_row_number for decision in result.decisions] == [
        1,
        2,
    ]
    assert all(
        decision.proposal_batch_id == result.batch.id and decision.status == "proposed"
        for decision in result.decisions
    )
    assert db_session.query(OntAssignmentCutoverProposalBatch).count() == 1
    assert db_session.query(OntAssignmentIdentityDecision).count() == 2
    assert all(assignment.active for assignment in assignments)
    projected = list_cutover_proposal_batches(db_session, query=str(result.batch.id))
    assert len(projected) == 1
    assert projected[0]["status"] == "awaiting independent review"
    assert len(projected[0]["decisions"]) == 2


def test_proposal_rolls_back_every_staged_row_on_mid_batch_failure(
    db_session, monkeypatch
):
    assignments = _drift_assignments(db_session, count=2)
    report, items = _deactivation_items(db_session)
    original = batch_owner.propose_assignment_identity_repair
    call_count = 0

    def fail_second(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise batch_owner.OntAssignmentIdentityError("injected second-row failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(batch_owner, "propose_assignment_identity_repair", fail_second)

    with pytest.raises(
        batch_owner.OntAssignmentIdentityError, match="second-row failure"
    ):
        _propose(db_session, report, items)

    assert db_session.query(OntAssignmentCutoverProposalBatch).count() == 0
    assert db_session.query(OntAssignmentIdentityDecision).count() == 0
    assert db_session.query(OntAssignment).count() == len(assignments)


def test_batch_proposer_cannot_self_review(db_session):
    _drift_assignments(db_session)
    report, items = _deactivation_items(db_session)
    proposal = _propose(db_session, report, items)

    with pytest.raises(OntAssignmentCutoverBatchError, match="proposer cannot review"):
        review_ont_assignment_cutover_batch(
            db_session,
            proposal.batch.id,
            expected_manifest_sha256=proposal.batch.manifest_sha256,
            action="approve",
            reviewed_by="planner@example.com",
            review_notes="Self review is forbidden",
        )

    assert proposal.decisions[0].status == "proposed"
    assert db_session.query(OntAssignmentCutoverBatchReview).count() == 0


def test_changed_report_blocks_atomic_approval(db_session):
    assignments = _drift_assignments(db_session, count=2)
    report, items = _deactivation_items(db_session)
    proposal = _propose(db_session, report, items)
    assignments[1].assigned_at = None
    db_session.commit()

    with pytest.raises(OntAssignmentCutoverBatchError, match="report changed"):
        review_ont_assignment_cutover_batch(
            db_session,
            proposal.batch.id,
            expected_manifest_sha256=proposal.batch.manifest_sha256,
            action="approve",
            reviewed_by="reviewer@example.com",
            review_notes="Every manifest row independently verified",
        )

    db_session.expire_all()
    decisions = db_session.query(OntAssignmentIdentityDecision).all()
    assert {decision.status for decision in decisions} == {"proposed"}
    assert db_session.query(OntAssignmentCutoverBatchReview).count() == 0
    assert all(assignment.active for assignment in assignments)


def test_review_rolls_back_every_decision_on_mid_batch_failure(db_session, monkeypatch):
    assignments = _drift_assignments(db_session, count=2)
    report, items = _deactivation_items(db_session)
    proposal = _propose(db_session, report, items)
    original = batch_owner.approve_assignment_identity_repair
    call_count = 0

    def fail_second(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise batch_owner.OntAssignmentIdentityError("injected second-row failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(batch_owner, "approve_assignment_identity_repair", fail_second)

    with pytest.raises(
        batch_owner.OntAssignmentIdentityError, match="second-row failure"
    ):
        review_ont_assignment_cutover_batch(
            db_session,
            proposal.batch.id,
            expected_manifest_sha256=proposal.batch.manifest_sha256,
            action="approve",
            reviewed_by="reviewer@example.com",
            review_notes="Every manifest row independently verified",
        )

    db_session.expire_all()
    assert {
        decision.status for decision in db_session.query(OntAssignmentIdentityDecision)
    } == {"proposed"}
    assert db_session.query(OntAssignmentCutoverBatchReview).count() == 0
    assert all(assignment.active for assignment in assignments)


def test_valid_batch_approval_changes_only_decision_state(db_session):
    assignments = _drift_assignments(db_session, count=2)
    report, items = _deactivation_items(db_session)
    proposal = _propose(db_session, report, items)

    reviewed = review_ont_assignment_cutover_batch(
        db_session,
        proposal.batch.id,
        expected_manifest_sha256=proposal.batch.manifest_sha256,
        action="approve",
        reviewed_by="reviewer@example.com",
        review_notes="Every manifest row independently verified",
    )
    replay = review_ont_assignment_cutover_batch(
        db_session,
        proposal.batch.id,
        expected_manifest_sha256=proposal.batch.manifest_sha256,
        action="approve",
        reviewed_by="reviewer@example.com",
        review_notes="Every manifest row independently verified",
    )

    assert reviewed.created is True
    assert replay.created is False
    assert {decision.status for decision in reviewed.decisions} == {"approved"}
    assert reviewed.review.action == "approve"
    assert all(assignment.active for assignment in assignments)
    assert db_session.query(OntAssignmentCutoverBatchReview).count() == 1


def test_batch_decline_preserves_all_evidence(db_session):
    assignments = _drift_assignments(db_session, count=2)
    report, items = _deactivation_items(db_session)
    proposal = _propose(db_session, report, items)

    reviewed = review_ont_assignment_cutover_batch(
        db_session,
        proposal.batch.id,
        expected_manifest_sha256=proposal.batch.manifest_sha256,
        action="decline",
        reviewed_by="reviewer@example.com",
        review_notes="Field evidence does not support this batch",
    )

    assert {decision.status for decision in reviewed.decisions} == {"declined"}
    assert all(
        decision.closed_reason == "assignment_identity_decision_declined"
        for decision in reviewed.decisions
    )
    assert all(assignment.active for assignment in assignments)


def test_batch_cli_and_owner_expose_no_execution_path(monkeypatch):
    from scripts.network import review_ont_assignment_cutover_batch as command

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "review_ont_assignment_cutover_batch.py",
            "inspect",
            "--batch-id",
            str(uuid.uuid4()),
        ],
    )
    args = command.parse_args()
    source = inspect.getsource(batch_owner)
    command_source = inspect.getsource(command)

    assert args.command == "inspect"
    assert "execute_assignment_identity_repair" not in source
    assert "def execute_" not in source
    assert 'add_parser("execute"' not in command_source
    assert "execute_assignment_identity_repair" not in command_source
    assert not hasattr(args, "execute")
    assert not hasattr(args, "apply")


def test_blocked_proposal_preserves_no_partial_rows(db_session):
    _drift_assignments(db_session)
    report, items = _deactivation_items(db_session)
    items[0]["finding_sha256"] = "a" * 64

    with pytest.raises(OntAssignmentCutoverBatchBlocked):
        _propose(db_session, report, items)

    assert db_session.query(OntAssignmentCutoverProposalBatch).count() == 0
    assert db_session.query(OntAssignmentIdentityDecision).count() == 0
