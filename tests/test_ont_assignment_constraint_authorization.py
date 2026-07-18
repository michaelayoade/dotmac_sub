from __future__ import annotations

import inspect
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.models.ont_assignment_constraint_authorization import (
    OntAssignmentConstraintAuthorizationRequest,
    OntAssignmentConstraintAuthorizationReview,
)
from app.services.network.ont_assignment_constraint_authorization import (
    OntAssignmentConstraintAuthorizationError,
    OntAssignmentConstraintAuthorizationRequestBlocked,
    OntAssignmentConstraintAuthorizationReviewBlocked,
    inspect_ont_assignment_constraint_authorizations,
    preview_ont_assignment_constraint_authorization_request,
    preview_ont_assignment_constraint_authorization_review,
    request_ont_assignment_constraint_authorization,
    review_ont_assignment_constraint_authorization,
)
from app.services.network.ont_assignment_cutover_coverage import (
    reconcile_ont_assignment_cutover_coverage,
)
from app.web.admin import network_fiber_plant

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
EXPIRY = NOW + timedelta(hours=4)


def _drift_assignment(db_session) -> OntAssignment:
    suffix = uuid.uuid4().hex[:10]
    olt = OLTDevice(
        name=f"Authorization OLT {suffix}",
        hostname=f"authorization-{suffix}.example.test",
        is_active=True,
    )
    db_session.add(olt)
    db_session.flush()
    pon = PonPort(olt_id=olt.id, name="0/1/1", is_active=True)
    db_session.add(pon)
    db_session.flush()
    ont = OntUnit(
        serial_number=f"AUTHORIZE-{suffix}",
        olt_device_id=olt.id,
        pon_port_id=pon.id,
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(
        ont_unit_id=ont.id,
        pon_port_id=pon.id,
        assigned_at=NOW,
        active=True,
    )
    db_session.add(assignment)
    db_session.commit()
    return assignment


def _request_preview(db_session):
    coverage = reconcile_ont_assignment_cutover_coverage(db_session)
    preview = preview_ont_assignment_constraint_authorization_request(
        db_session,
        expected_coverage_report_sha256=coverage.coverage_report_sha256,
        expected_cutover_report_sha256=coverage.cutover_report_sha256,
        target_environment="test.sub.example",
        expires_at=EXPIRY,
        requested_by="requester@example.com",
        reason="Authorize a separately reviewed future constraint change",
        now=NOW,
    )
    return coverage, preview


def _request(db_session):
    coverage, preview = _request_preview(db_session)
    result = request_ont_assignment_constraint_authorization(
        db_session,
        expected_coverage_report_sha256=coverage.coverage_report_sha256,
        expected_cutover_report_sha256=coverage.cutover_report_sha256,
        expected_request_sha256=preview.request_sha256,
        target_environment="test.sub.example",
        expires_at=EXPIRY,
        requested_by="requester@example.com",
        reason="Authorize a separately reviewed future constraint change",
        now=NOW,
    )
    return coverage, preview, result


def _review_preview(db_session, request, *, action="approve", now=NOW):
    return preview_ont_assignment_constraint_authorization_review(
        db_session,
        request.id,
        expected_request_sha256=request.request_sha256,
        action=action,
        reviewed_by="reviewer@example.com",
        review_notes="Exact clean evidence independently reviewed",
        now=now,
    )


def _review(db_session, request, preview, *, now=NOW):
    return review_ont_assignment_constraint_authorization(
        db_session,
        request.id,
        expected_request_sha256=request.request_sha256,
        expected_attestation_sha256=preview.attestation_sha256,
        action=preview.action,
        reviewed_by="reviewer@example.com",
        review_notes="Exact clean evidence independently reviewed",
        now=now,
    )


def test_request_requires_exact_clean_coverage_without_writing(db_session):
    _drift_assignment(db_session)
    coverage = reconcile_ont_assignment_cutover_coverage(db_session)

    preview = preview_ont_assignment_constraint_authorization_request(
        db_session,
        expected_coverage_report_sha256=coverage.coverage_report_sha256,
        expected_cutover_report_sha256=coverage.cutover_report_sha256,
        target_environment="test.sub.example",
        expires_at=EXPIRY,
        requested_by="requester@example.com",
        reason="Blocked dirty evidence must remain visible",
        now=NOW,
    )

    assert preview.ready is False
    assert "coverage_not_ready_for_authorization_review" in {
        blocker["code"] for blocker in preview.blockers
    }
    with pytest.raises(OntAssignmentConstraintAuthorizationRequestBlocked):
        request_ont_assignment_constraint_authorization(
            db_session,
            expected_coverage_report_sha256=coverage.coverage_report_sha256,
            expected_cutover_report_sha256=coverage.cutover_report_sha256,
            expected_request_sha256=preview.request_sha256,
            target_environment="test.sub.example",
            expires_at=EXPIRY,
            requested_by="requester@example.com",
            reason="Blocked dirty evidence must remain visible",
            now=NOW,
        )
    assert db_session.query(OntAssignmentConstraintAuthorizationRequest).count() == 0


def test_exact_request_is_immutable_evidence_and_idempotent(db_session):
    coverage, preview, result = _request(db_session)
    replay = request_ont_assignment_constraint_authorization(
        db_session,
        expected_coverage_report_sha256=coverage.coverage_report_sha256,
        expected_cutover_report_sha256=coverage.cutover_report_sha256,
        expected_request_sha256=preview.request_sha256,
        target_environment="test.sub.example",
        expires_at=EXPIRY,
        requested_by="requester@example.com",
        reason="Authorize a separately reviewed future constraint change",
        now=NOW,
    )

    assert preview.ready is True
    assert result.created is True
    assert replay.created is False
    assert replay.request.id == result.request.id
    assert result.request.coverage_payload["coverage_report_sha256"] == (
        coverage.coverage_report_sha256
    )
    assert result.request.target_environment == "test.sub.example"
    assert db_session.query(OntAssignmentConstraintAuthorizationRequest).count() == 1


def test_request_confirmation_rejects_changed_coverage(db_session):
    coverage, preview = _request_preview(db_session)
    _drift_assignment(db_session)

    with pytest.raises(
        OntAssignmentConstraintAuthorizationError, match="changed after preview"
    ):
        request_ont_assignment_constraint_authorization(
            db_session,
            expected_coverage_report_sha256=coverage.coverage_report_sha256,
            expected_cutover_report_sha256=coverage.cutover_report_sha256,
            expected_request_sha256=preview.request_sha256,
            target_environment="test.sub.example",
            expires_at=EXPIRY,
            requested_by="requester@example.com",
            reason="Authorize a separately reviewed future constraint change",
            now=NOW,
        )


def test_independent_approval_is_exact_idempotent_and_current(db_session):
    _coverage, _preview, requested = _request(db_session)
    preview = _review_preview(db_session, requested.request)
    result = _review(db_session, requested.request, preview)
    replay = _review(db_session, requested.request, preview)
    inspection = inspect_ont_assignment_constraint_authorizations(
        db_session, target_environment="test.sub.example", now=NOW
    )

    assert preview.ready is True
    assert result.created is True
    assert replay.created is False
    assert replay.review.id == result.review.id
    assert inspection["current_approval_count"] == 1
    assert inspection["authorizations"][0]["state"] == ("approved_current_evidence")
    assert (
        inspection["authorizations"][0]["eligible_for_separate_ddl_change_review"]
        is True
    )
    assert inspection["ddl_authority"] is False


def test_requester_cannot_review_own_authorization(db_session):
    _coverage, _preview, requested = _request(db_session)

    preview = preview_ont_assignment_constraint_authorization_review(
        db_session,
        requested.request.id,
        expected_request_sha256=requested.request.request_sha256,
        action="approve",
        reviewed_by="requester@example.com",
        review_notes="Self-review must fail",
        now=NOW,
    )

    assert preview.ready is False
    assert "authorization_reviewer_not_independent" in {
        blocker["code"] for blocker in preview.blockers
    }


def test_expired_request_blocks_approval_but_can_be_declined(db_session):
    _coverage, _preview, requested = _request(db_session)
    after_expiry = EXPIRY + timedelta(seconds=1)
    approval = _review_preview(
        db_session, requested.request, action="approve", now=after_expiry
    )
    decline = _review_preview(
        db_session, requested.request, action="decline", now=after_expiry
    )

    assert approval.ready is False
    assert "authorization_request_expired" in {
        blocker["code"] for blocker in approval.blockers
    }
    with pytest.raises(OntAssignmentConstraintAuthorizationReviewBlocked):
        _review(db_session, requested.request, approval, now=after_expiry)
    result = _review(db_session, requested.request, decline, now=after_expiry)
    inspection = inspect_ont_assignment_constraint_authorizations(
        db_session, now=after_expiry
    )

    assert result.review.action == "decline"
    assert inspection["authorizations"][0]["state"] == "declined"
    assert inspection["current_approval_count"] == 0


def test_coverage_drift_blocks_approval_but_not_explicit_decline(db_session):
    _coverage, _preview, requested = _request(db_session)
    _drift_assignment(db_session)

    approval = _review_preview(db_session, requested.request, action="approve")
    decline = _review_preview(db_session, requested.request, action="decline")
    inspection = inspect_ont_assignment_constraint_authorizations(db_session, now=NOW)

    assert approval.ready is False
    assert {
        "authorization_request_evidence_stale",
        "coverage_not_ready_for_authorization_review",
    }.issubset({blocker["code"] for blocker in approval.blockers})
    assert decline.ready is True
    assert inspection["authorizations"][0]["state"] == "pending_stale"


def test_current_approval_becomes_stale_without_mutating_evidence(db_session):
    _coverage, _preview, requested = _request(db_session)
    review_preview = _review_preview(db_session, requested.request)
    review = _review(db_session, requested.request, review_preview).review
    attestation_sha256 = review.attestation_sha256
    _drift_assignment(db_session)

    inspection = inspect_ont_assignment_constraint_authorizations(db_session, now=NOW)

    assert inspection["authorizations"][0]["state"] == "approved_stale"
    assert inspection["current_approval_count"] == 0
    db_session.refresh(review)
    assert review.attestation_sha256 == attestation_sha256


def test_review_confirmation_and_stored_evidence_fail_closed(db_session):
    _coverage, _preview, requested = _request(db_session)
    review_preview = _review_preview(db_session, requested.request)

    with pytest.raises(
        OntAssignmentConstraintAuthorizationError, match="changed after preview"
    ):
        review_ont_assignment_constraint_authorization(
            db_session,
            requested.request.id,
            expected_request_sha256=requested.request.request_sha256,
            expected_attestation_sha256=review_preview.attestation_sha256,
            action="approve",
            reviewed_by="reviewer@example.com",
            review_notes="Different confirmation evidence",
            now=NOW,
        )

    requested.request.coverage_payload = {
        **requested.request.coverage_payload,
        "tampered": True,
    }
    db_session.commit()
    inspection = inspect_ont_assignment_constraint_authorizations(db_session, now=NOW)
    assert inspection["authorizations"][0]["state"] == "invalid_evidence"
    assert db_session.query(OntAssignmentConstraintAuthorizationReview).count() == 0


def test_cli_and_admin_projection_expose_no_ddl_or_execution_path(monkeypatch):
    from app.services.network import ont_assignment_constraint_authorization as owner
    from scripts.network import (
        review_ont_assignment_constraint_authorization as command,
    )

    monkeypatch.setattr(
        sys,
        "argv",
        ["review_ont_assignment_constraint_authorization.py", "inspect"],
    )
    args = command.parse_args()
    service_source = inspect.getsource(owner)
    command_source = inspect.getsource(command)
    paths = {route.path: route for route in network_fiber_plant.router.routes}
    route = paths["/network/ont-assignment-constraint-authorizations"]
    template = Path(
        "templates/admin/network/fiber/ont_assignment_constraint_authorizations.html"
    )

    assert args.command == "inspect"
    assert route.methods == {"GET"}
    assert template.exists()
    assert "execute_assignment_identity_repair" not in service_source
    assert "OntAssignment(" not in service_source
    assert 'add_parser("execute"' not in command_source
    assert 'add_parser("apply"' not in command_source
    assert "enable_constraint" not in service_source
    assert "has no mutation controls" in template.read_text()
