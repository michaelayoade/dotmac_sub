from __future__ import annotations

import hashlib
import inspect
import json
import sys
import uuid
from datetime import UTC, datetime

import pytest

from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.models.ont_assignment_cutover import (
    OntAssignmentCutoverVerificationAttestation,
)
from app.services.network import ont_assignment_cutover_verification as owner
from app.services.network.ont_assignment_cutover import audit_ont_assignment_cutover
from app.services.network.ont_assignment_cutover_batches import (
    propose_ont_assignment_cutover_batch,
    review_ont_assignment_cutover_batch,
)
from app.services.network.ont_assignment_cutover_verification import (
    OntAssignmentCutoverVerificationBlocked,
    OntAssignmentCutoverVerificationError,
    attest_ont_assignment_cutover_verification,
    inspect_ont_assignment_cutover_verifications,
    preview_ont_assignment_cutover_verification,
)
from app.services.network.ont_assignment_identity import (
    execute_assignment_identity_repair,
)
from app.services.web_network_ont_identity_reviews import (
    list_cutover_proposal_batches,
)


def _drift_assignment(db_session) -> OntAssignment:
    suffix = uuid.uuid4().hex[:10]
    olt = OLTDevice(
        name=f"Verification OLT {suffix}",
        hostname=f"verification-{suffix}.example.test",
        is_active=True,
    )
    db_session.add(olt)
    db_session.flush()
    pon = PonPort(olt_id=olt.id, name="0/1/1", is_active=True)
    db_session.add(pon)
    db_session.flush()
    ont = OntUnit(
        serial_number=f"VERIFY-{suffix}",
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
    db_session.commit()
    return assignment


def _reviewed_batch(db_session, *, action: str = "approve"):
    assignment = _drift_assignment(db_session)
    report = audit_ont_assignment_cutover(db_session)
    finding = next(
        finding for finding in report.findings if finding.assignment_id == assignment.id
    )
    proposal = propose_ont_assignment_cutover_batch(
        db_session,
        expected_report_sha256=report.report_sha256,
        items=[
            {
                "action": "deactivate",
                "assignment_id": str(assignment.id),
                "duplicate_assignment_ids": [],
                "finding_sha256": finding.input_sha256,
                "reason": "Field evidence confirms this assignment is stale",
            }
        ],
        proposed_by="planner@example.com",
        reason="Reviewed post-execution verification fixture",
        source_name="phase-13-test",
    )
    reviewed = review_ont_assignment_cutover_batch(
        db_session,
        proposal.batch.id,
        expected_manifest_sha256=proposal.batch.manifest_sha256,
        action=action,
        reviewed_by="reviewer@example.com",
        review_notes="Exact batch evidence independently reviewed",
    )
    return assignment, proposal, reviewed


def _preview(db_session, proposal, *, actor: str = "verifier@example.com"):
    return preview_ont_assignment_cutover_verification(
        db_session,
        proposal.batch.id,
        expected_manifest_sha256=proposal.batch.manifest_sha256,
        verified_by=actor,
        verification_notes="Terminal results and fresh audit independently checked",
    )


def _attest(db_session, proposal, preview):
    return attest_ont_assignment_cutover_verification(
        db_session,
        proposal.batch.id,
        expected_manifest_sha256=proposal.batch.manifest_sha256,
        expected_evidence_sha256=preview.evidence_sha256,
        verified_by="verifier@example.com",
        verification_notes="Terminal results and fresh audit independently checked",
    )


def test_pending_decisions_are_visible_but_cannot_be_attested(db_session):
    assignment, proposal, _reviewed = _reviewed_batch(db_session)

    preview = _preview(db_session, proposal)

    assert preview.ready is False
    assert preview.outcome == "pending"
    assert preview.counts["pending"] == 1
    assert preview.batch_scope_residual_findings[0]["assignment_id"] == str(
        assignment.id
    )
    with pytest.raises(OntAssignmentCutoverVerificationBlocked):
        _attest(db_session, proposal, preview)
    assert db_session.query(OntAssignmentCutoverVerificationAttestation).count() == 0


def test_applied_clean_scope_attestation_is_exact_and_idempotent(db_session):
    assignment, proposal, reviewed = _reviewed_batch(db_session)
    decision = reviewed.decisions[0]
    execute_assignment_identity_repair(
        db_session, decision.id, executed_by="executor@example.com"
    )
    db_session.refresh(assignment)
    assert assignment.active is False

    preview = _preview(db_session, proposal)
    result = _attest(db_session, proposal, preview)
    replay = _attest(db_session, proposal, preview)

    assert preview.ready is True
    assert preview.outcome == "applied_clean_scope"
    assert preview.counts["applied"] == 1
    assert preview.batch_scope_residual_findings == ()
    assert preview.global_cutover_ready is True
    assert result.created is True
    assert replay.created is False
    assert replay.attestation.id == result.attestation.id
    assert (
        result.attestation.evidence_payload["decisions"][0]["result_sha256"]
        == decision.result_sha256
    )
    assert assignment.active is False
    projected = list_cutover_proposal_batches(db_session, query=str(proposal.batch.id))
    assert projected[0]["latest_verification"].id == result.attestation.id


@pytest.mark.parametrize(
    "actor",
    ["planner@example.com", "reviewer@example.com", "executor@example.com"],
)
def test_verifier_must_be_independent_of_proposal_review_and_execution(
    db_session, actor
):
    _assignment, proposal, reviewed = _reviewed_batch(db_session)
    execute_assignment_identity_repair(
        db_session,
        reviewed.decisions[0].id,
        executed_by="executor@example.com",
    )

    preview = _preview(db_session, proposal, actor=actor)

    assert preview.ready is False
    assert "verification_actor_not_independent" in {
        blocker["code"] for blocker in preview.blockers
    }


def test_stale_closed_result_and_residual_finding_remain_distinct(db_session):
    assignment, proposal, reviewed = _reviewed_batch(db_session)
    assignment.assigned_at = None
    db_session.commit()
    decision = execute_assignment_identity_repair(
        db_session,
        reviewed.decisions[0].id,
        executed_by="executor@example.com",
    )

    preview = _preview(db_session, proposal)
    result = _attest(db_session, proposal, preview)

    assert decision.status == "closed"
    assert preview.ready is True
    assert preview.outcome == "completed_with_stale_closures"
    assert preview.counts["stale_closed"] == 1
    assert len(preview.batch_scope_residual_findings) == 1
    assert result.attestation.stale_closed_count == 1
    assert result.attestation.batch_scope_residual_count == 1
    assert result.attestation.global_cutover_ready is False


def test_applied_result_does_not_hide_later_batch_scope_drift(db_session):
    assignment, proposal, reviewed = _reviewed_batch(db_session)
    execute_assignment_identity_repair(
        db_session,
        reviewed.decisions[0].id,
        executed_by="executor@example.com",
    )
    assignment.active = True
    db_session.commit()

    preview = _preview(db_session, proposal)
    result = _attest(db_session, proposal, preview)

    assert preview.outcome == "applied_with_residual_findings"
    assert preview.counts["applied"] == 1
    assert len(preview.batch_scope_residual_findings) == 1
    assert result.attestation.applied_count == 1
    assert result.attestation.batch_scope_residual_count == 1


def test_conflict_closed_result_is_not_classified_as_stale(db_session):
    _assignment, proposal, reviewed = _reviewed_batch(db_session)
    decision = reviewed.decisions[0]
    result_payload = {
        "decision_id": str(decision.id),
        "error": "canonical assignment identity uniqueness conflict",
        "outcome": "closed_conflict",
    }
    decision.status = "closed"
    decision.executed_by = "executor@example.com"
    decision.executed_at = datetime.now(UTC)
    decision.closed_reason = "canonical_assignment_identity_conflict"
    decision.result_payload = result_payload
    decision.result_sha256 = hashlib.sha256(
        json.dumps(result_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    db_session.commit()

    preview = _preview(db_session, proposal)

    assert preview.ready is True
    assert preview.outcome == "completed_with_conflict_closures"
    assert preview.counts["conflict_closed"] == 1
    assert preview.counts["stale_closed"] == 0


def test_declined_batch_attestation_does_not_claim_cleanup(db_session):
    assignment, proposal, _reviewed = _reviewed_batch(db_session, action="decline")

    preview = _preview(db_session, proposal)
    result = _attest(db_session, proposal, preview)

    assert preview.ready is True
    assert preview.outcome == "declined"
    assert preview.counts["declined"] == 1
    assert len(preview.batch_scope_residual_findings) == 1
    assert result.attestation.outcome == "declined"
    assert assignment.active is True


def test_terminal_result_digest_mismatch_blocks_attestation(db_session):
    _assignment, proposal, reviewed = _reviewed_batch(db_session)
    decision = execute_assignment_identity_repair(
        db_session,
        reviewed.decisions[0].id,
        executed_by="executor@example.com",
    )
    decision.result_payload = {**decision.result_payload, "tampered": True}
    db_session.commit()

    preview = _preview(db_session, proposal)

    assert preview.ready is False
    assert "terminal_decision_result_digest_mismatch" in {
        blocker["code"] for blocker in preview.blockers
    }


def test_changed_fresh_audit_rejects_stale_evidence_digest(db_session):
    _assignment, proposal, reviewed = _reviewed_batch(db_session)
    execute_assignment_identity_repair(
        db_session,
        reviewed.decisions[0].id,
        executed_by="executor@example.com",
    )
    preview = _preview(db_session, proposal)
    _drift_assignment(db_session)

    with pytest.raises(
        OntAssignmentCutoverVerificationError, match="changed after preview"
    ):
        _attest(db_session, proposal, preview)

    assert db_session.query(OntAssignmentCutoverVerificationAttestation).count() == 0


def test_inspection_preserves_multiple_exact_verification_snapshots(db_session):
    _assignment, proposal, reviewed = _reviewed_batch(db_session)
    execute_assignment_identity_repair(
        db_session,
        reviewed.decisions[0].id,
        executed_by="executor@example.com",
    )
    first_preview = _preview(db_session, proposal)
    first = _attest(db_session, proposal, first_preview)
    _drift_assignment(db_session)
    second_preview = _preview(db_session, proposal)
    second = _attest(db_session, proposal, second_preview)

    inspection = inspect_ont_assignment_cutover_verifications(
        db_session, proposal.batch.id
    )

    assert first.attestation.id != second.attestation.id
    assert len(inspection["verification_attestations"]) == 2
    assert second.attestation.global_cutover_ready is False
    assert second.attestation.batch_scope_residual_count == 0


def test_verification_owner_and_cli_expose_no_mutation_or_constraint_path(
    monkeypatch,
):
    from scripts.network import verify_ont_assignment_cutover_batch as command

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_ont_assignment_cutover_batch.py",
            "inspect",
            "--batch-id",
            str(uuid.uuid4()),
        ],
    )
    args = command.parse_args()
    source = inspect.getsource(owner)
    command_source = inspect.getsource(command)

    assert args.command == "inspect"
    assert "execute_assignment_identity_repair" not in source
    assert "OntAssignment(" not in source
    assert "def execute_" not in source
    assert 'add_parser("execute"' not in command_source
    assert 'add_parser("apply"' not in command_source
    assert "enable_constraint" not in source
