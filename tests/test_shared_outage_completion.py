# ruff: noqa: F811
"""Fast owner regressions; migrated concurrency evidence lives in the PG lane."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.network_monitoring import (
    OutageIncident,
    OutageIncidentTicketLink,
    OutageIncidentWorkOrderLink,
    OutageScopeRevision,
    OutageScopeRevisionMember,
)
from app.models.outage_complaint_correlation import OutageComplaintCorrelationReceipt
from app.models.support import Ticket
from app.models.work_order import WorkOrder
from app.schemas.support import TicketUpdate
from app.services import support
from app.services.domain_errors import DomainError
from app.services.field import completion_review
from app.services.field.completion_review_contracts import (
    CompletionBasisParticipant,
    SupportTicketCompletionPreviewQuery,
)
from app.services.support_outage_resolution import _material, _validate_reviews
from app.services.topology import outage, outage_completion
from app.services.topology.outage_completion_contracts import (
    ComplaintResolutionScopesPreviewQuery,
    EmptySharedScopeReview,
    OutageReplacementCoverageQuery,
    ReviewedComplaintResolutionScope,
)
from tests.services.topology.test_outage_complaint_correlation import (
    reviewed,  # noqa: F401
)


@pytest.fixture
def shared(db_session, reviewed, monkeypatch):
    actor = outage._complaint_staff(
        db_session,
        support=reviewed.support_access,
        monitoring=reviewed.monitoring_access,
        write=True,
    )
    monkeypatch.setattr(
        outage_completion, "revalidate_staff_admission", lambda *a, **k: actor
    )
    monkeypatch.setattr(
        completion_review, "revalidate_staff_admission", lambda *a, **k: actor
    )
    db_session.commit()
    linked = outage.link_complaint_ticket(db_session, command=reviewed)
    infra = Ticket(title="Repair shared feeder", status="open")
    db_session.add(infra)
    db_session.flush()
    db_session.add(
        OutageIncidentTicketLink(
            incident_id=reviewed.incident_id,
            ticket_id=infra.id,
            role="infrastructure",
            source="operator",
        )
    )
    incident = db_session.get(OutageIncident, reviewed.incident_id)
    incident.status = "resolved"
    incident.resolved_at = datetime.now(UTC)
    db_session.commit()
    return reviewed, linked, infra.id


def snapshot(db, command):
    return outage_completion.preview_complaint_resolution_scopes(
        db,
        query=ComplaintResolutionScopesPreviewQuery(
            command.ticket_id, command.support_access
        ),
    ).incidents


def issue(db, command, infra, *, revision=None):
    revision = revision or db.get(OutageScopeRevision, command.expected_revision_id)
    work = WorkOrder(
        title="Shared feeder visit",
        origin_ticket_id=infra,
        subscriber_id=None,
        work_order_kind="infrastructure",
        purpose="repair",
        access_technology="fiber",
        template_version=1,
        classification_source="explicit_operator_selection",
    )
    db.add(work)
    db.flush()
    row = OutageIncidentWorkOrderLink(
        incident_id=command.incident_id,
        work_order_id=work.id,
        scope_revision_id=revision.id,
        scope_revision_sequence=revision.sequence,
        membership_token=revision.membership_token,
        target_type=revision.new_scope_type,
        target_id=revision.new_scope_id,
        idempotency_key=str(uuid4()),
        command_fingerprint="a" * 64,
        created_by=str(command.support_access.principal_id),
    )
    db.add(row)
    db.commit()
    return row


def review(scope):
    return ReviewedComplaintResolutionScope(
        scope.complaint_link_id,
        scope.reviewed_revision_id,
        scope.membership_token,
        scope.resolved_at,
        tuple(x.issuance_link_id for x in scope.issuances),
        EmptySharedScopeReview.restored_without_issued_field_scope
        if not scope.issuances
        else None,
    )


def test_empty_shared_scope_requires_explicit_review_and_blocks_direct_close(
    db_session, shared
):
    command, _, _ = shared
    scopes = snapshot(db_session, command)
    assert len(scopes) == 1 and scopes[0].issuances == ()
    with pytest.raises(DomainError, match="Explicitly review"):
        _validate_reviews(scopes=scopes, reviews=())
    _validate_reviews(scopes=scopes, reviews=(review(scopes[0]),))
    db_session.commit()
    with pytest.raises(DomainError, match="confirmation"):
        support.Tickets.update(
            db_session, str(command.ticket_id), TicketUpdate(status="closed")
        )


def test_every_actual_matching_issued_job_requires_enrollment(db_session, shared):
    command, _, infra = shared
    first = issue(db_session, command, infra)
    second = issue(db_session, command, infra)
    scopes = snapshot(db_session, command)
    assert {x.issuance_link_id for x in scopes[0].issuances} == {first.id, second.id}
    with pytest.raises(DomainError) as error:
        completion_review.preview_ticket_completion_basis(
            db_session,
            query=SupportTicketCompletionPreviewQuery(
                ticket_id=command.ticket_id, access=command.support_access
            ),
        )
    assert error.value.code.endswith("enrollment_required")
    partial = replace(review(scopes[0]), issuance_link_ids=(first.id,))
    with pytest.raises(DomainError, match="changed"):
        _validate_reviews(scopes=scopes, reviews=(partial,))


@pytest.mark.parametrize(
    "drift", ["reopen", "re_resolve", "new_issue", "service_rebind"]
)
def test_confirmation_material_changes_or_refuses_on_drift(db_session, shared, drift):
    from app.models.catalog import Subscription
    from app.models.subscriber import Subscriber

    command, _, infra = shared
    before = _material(snapshot(db_session, command))
    db_session.commit()
    incident = db_session.get(OutageIncident, command.incident_id)
    if drift == "reopen":
        incident.status = "open"
    elif drift == "re_resolve":
        incident.resolved_at = datetime.now(UTC)
    elif drift == "new_issue":
        issue(db_session, command, infra)
    else:
        other = Subscriber(
            first_name="Other", last_name="Customer", email=f"{uuid4()}@test.example"
        )
        db_session.add(other)
        db_session.flush()
        db_session.get(Subscription, command.subscription_id).subscriber_id = other.id
    db_session.commit()
    if drift in {"reopen", "service_rebind"}:
        with pytest.raises(DomainError):
            snapshot(db_session, command)
    else:
        assert _material(snapshot(db_session, command)) != before


def test_audience_growth_is_explicit_full_source_coverage(db_session, shared):
    command, _, infra = shared
    first = issue(db_session, command, infra)
    revision = OutageScopeRevision(
        incident_id=command.incident_id,
        sequence=2,
        effective_at=datetime.now(UTC),
        reason="audience_drift",
        new_scope_type="unresolved",
        membership_token="b" * 64,
        member_count=2,
    )
    db_session.add(revision)
    db_session.flush()
    db_session.add_all(
        [
            OutageScopeRevisionMember(
                revision_id=revision.id, subscription_id=x, membership="retained"
            )
            for x in (command.subscription_id, uuid4())
        ]
    )
    db_session.commit()
    second = issue(db_session, command, infra, revision=revision)
    snapshot(db_session, command)
    proof = outage_completion._replacement_coverage(
        db_session,
        query=OutageReplacementCoverageQuery(first.work_order_id, second.work_order_id),
    )
    assert proof.source.issued_revision_id != proof.replacement.issued_revision_id
    member = db_session.scalar(
        select(OutageScopeRevisionMember).where(
            OutageScopeRevisionMember.revision_id == revision.id,
            OutageScopeRevisionMember.subscription_id == command.subscription_id,
        )
    )
    member.membership = "left"
    db_session.commit()
    snapshot(db_session, command)
    with pytest.raises(DomainError, match="every service"):
        outage_completion._replacement_coverage(
            db_session,
            query=OutageReplacementCoverageQuery(
                first.work_order_id, second.work_order_id
            ),
        )


def test_explicit_legacy_adoption_preserves_history_and_can_review_resolved_incident(
    db_session, reviewed
):
    legacy = OutageIncidentTicketLink(
        incident_id=reviewed.incident_id,
        ticket_id=reviewed.ticket_id,
        role="complaint",
        source="operator",
        scope_revision_sequence=None,
        linked_by="historical",
    )
    db_session.add(legacy)
    db_session.get(OutageIncident, reviewed.incident_id).status = "resolved"
    db_session.commit()
    with pytest.raises(DomainError, match="no native proof"):
        outage.link_complaint_ticket(db_session, command=reviewed)
    adopted = outage.link_complaint_ticket(
        db_session, command=replace(reviewed, adopt_legacy=True)
    )
    db_session.refresh(legacy)
    receipt = db_session.scalar(
        select(OutageComplaintCorrelationReceipt).where(
            OutageComplaintCorrelationReceipt.link_id == legacy.id
        )
    )
    assert (
        adopted.link_id == legacy.id
        and legacy.linked_by == "historical"
        and legacy.source == "operator"
        and legacy.scope_revision_sequence is None
    )
    assert (
        receipt.proof_origin == "reviewed_adoption"
        and receipt.original_revision_id == reviewed.expected_revision_id
    )


def test_empty_required_set_does_not_expand_unrelated_graph(db_session, monkeypatch):
    monkeypatch.setattr(
        completion_review, "revalidate_staff_admission", lambda *a, **k: object()
    )
    # Internal kernel receives an already admitted, exact empty marker set.
    assert (
        completion_review._scoped_completion_basis_locked(
            db_session,
            required_work_order_ids=(),
            participant=CompletionBasisParticipant.project_completion,
            expected_enrollment_ids=(),
            access=None,
        ).entries
        == ()
    )
