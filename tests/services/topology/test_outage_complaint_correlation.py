"""Fast non-authoritative unit evidence for the reviewed complaint owner."""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.catalog import Subscription
from app.models.event_store import EventStore
from app.models.network_monitoring import (
    OutageIncident,
    OutageIncidentTicketLink,
    OutageScopeRevision,
    OutageScopeRevisionMember,
)
from app.models.outage_complaint_correlation import OutageComplaintCorrelationReceipt
from app.models.subscriber import Subscriber
from app.models.support import Ticket
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.staff_permission_admission import AuthorizedStaffPerson
from app.services.topology import outage
from app.services.topology.outage_complaint_contracts import (
    MONITORING_READ,
    MONITORING_WRITE,
    SUPPORT_READ,
    SUPPORT_WRITE,
    ComplaintIncidentCandidatesQuery,
    ComplaintIncidentLinkCommand,
)


@pytest.fixture
def reviewed(db_session, catalog_offer, monkeypatch):
    from tests.staff_identity_fixtures import add_bound_staff_user

    user, person = add_bound_staff_user(db_session)
    actor = AuthorizedStaffPerson(user.id, person.id)
    monkeypatch.setattr(
        outage, "revalidate_staff_admission", lambda *args, **kwargs: actor
    )
    monkeypatch.setattr(outage, "has_permission", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        outage, "active_leave_restriction", lambda *args, **kwargs: None
    )
    subscriber = Subscriber(
        first_name="Review", last_name="Customer", email=f"{uuid4()}@test.example"
    )
    db_session.add(subscriber)
    db_session.flush()
    subscription = Subscription(subscriber_id=subscriber.id, offer_id=catalog_offer.id)
    ticket = Ticket(
        title="Customer connection complaint",
        subscriber_id=subscriber.id,
        status="open",
    )
    incident = OutageIncident(status="confirmed", note="Feeder interruption")
    db_session.add_all([subscription, ticket, incident])
    db_session.flush()
    revision = OutageScopeRevision(
        incident_id=incident.id,
        sequence=1,
        effective_at=outage.datetime.now(outage.UTC),
        reason="confirmed",
        new_scope_type="unresolved",
        membership_token="a" * 64,
        member_count=1,
    )
    db_session.add(revision)
    db_session.flush()
    db_session.add(
        OutageScopeRevisionMember(
            revision_id=revision.id,
            subscription_id=subscription.id,
            membership="entered",
        )
    )
    access = PermissionAdmission(
        actor.system_user_id,
        "system_user",
        frozenset({SUPPORT_READ, SUPPORT_WRITE, MONITORING_READ, MONITORING_WRITE}),
    )
    reason = "Reviewed exact customer service in persisted incident audience"
    command = ComplaintIncidentLinkCommand(
        context=CommandContext.system(
            actor=str(actor.system_user_id),
            scope=SUPPORT_WRITE,
            reason=reason,
            idempotency_key=str(uuid4()),
        ),
        support_access=access,
        monitoring_access=access,
        ticket_id=ticket.id,
        incident_id=incident.id,
        subscription_id=subscription.id,
        expected_revision_id=revision.id,
        expected_membership_token=revision.membership_token,
        reason=reason,
    )
    db_session.commit()
    return command


def _release(db):
    from app.services.db_session_adapter import db_session_adapter

    db_session_adapter.release_read_transaction(db)


def test_exact_replay_survives_revision_and_terminal_drift(db_session, reviewed):
    first = outage.link_complaint_ticket(db_session, command=reviewed)
    incident = db_session.get(OutageIncident, reviewed.incident_id)
    incident.status = "resolved"
    revision = OutageScopeRevision(
        incident_id=incident.id,
        sequence=2,
        effective_at=outage.datetime.now(outage.UTC),
        reason="audience_drift",
        new_scope_type="unresolved",
        membership_token="b" * 64,
        member_count=0,
    )
    db_session.add(revision)
    db_session.commit()
    replay = outage.link_complaint_ticket(db_session, command=reviewed)
    assert replay.link_id == first.link_id and replay.replayed
    assert replay.reviewed_revision_id == reviewed.expected_revision_id
    assert db_session.query(OutageIncidentTicketLink).count() == 1
    assert db_session.query(OutageComplaintCorrelationReceipt).count() == 1
    events = db_session.scalars(
        select(EventStore).where(EventStore.event_type == "outage.complaint_linked.v1")
    ).all()
    assert len(events) == 1
    assert events[0].payload["reviewed_revision_id"] == str(
        reviewed.expected_revision_id
    )


def test_changed_retry_reason_conflicts(db_session, reviewed):
    outage.link_complaint_ticket(db_session, command=reviewed)
    changed = replace(
        reviewed,
        reason="Changed review",
        context=replace(reviewed.context, reason="Changed review"),
    )
    with pytest.raises(DomainError, match="different reviewed input"):
        outage.link_complaint_ticket(db_session, command=changed)


@pytest.mark.parametrize(
    "change,code",
    [
        ("revision", "stale_revision"),
        ("subscription", "scope_mismatch"),
        ("terminal", "incident_ineligible"),
        ("ticket", "ticket_ineligible"),
        ("identity", "customer_ambiguous"),
    ],
)
def test_invalid_evidence_writes_nothing(db_session, reviewed, change, code):
    command = reviewed
    if change == "revision":
        command = replace(command, expected_revision_id=uuid4())
    elif change == "subscription":
        command = replace(command, subscription_id=uuid4())
    elif change == "terminal":
        db_session.get(OutageIncident, command.incident_id).status = "resolved"
    elif change == "ticket":
        db_session.get(Ticket, command.ticket_id).status = "closed"
    else:
        other = Subscriber(
            first_name="Other", last_name="Customer", email=f"{uuid4()}@test.example"
        )
        db_session.add(other)
        db_session.flush()
        db_session.get(Ticket, command.ticket_id).customer_account_id = other.id
    db_session.commit()
    with pytest.raises(DomainError) as caught:
        outage.link_complaint_ticket(db_session, command=command)
    assert caught.value.code.endswith(code)
    assert db_session.query(OutageIncidentTicketLink).count() == 0
    assert db_session.query(OutageComplaintCorrelationReceipt).count() == 0


def test_existing_pair_preserves_reviewed_provenance(db_session, reviewed):
    first = outage.link_complaint_ticket(db_session, command=reviewed)
    command = replace(
        reviewed, context=replace(reviewed.context, idempotency_key=str(uuid4()))
    )
    second = outage.link_complaint_ticket(db_session, command=command)
    assert (
        second.already_linked
        and not second.replayed
        and second.link_id == first.link_id
    )
    assert (
        db_session.query(EventStore)
        .filter_by(event_type="outage.complaint_linked.v1")
        .count()
        == 1
    )


def test_atomic_event_failure_rolls_back(db_session, reviewed, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("event unavailable")

    monkeypatch.setattr(outage, "emit_event", fail)
    with pytest.raises(RuntimeError, match="event unavailable"):
        outage.link_complaint_ticket(db_session, command=reviewed)
    assert db_session.query(OutageIncidentTicketLink).count() == 0
    assert db_session.query(OutageComplaintCorrelationReceipt).count() == 0
    assert (
        db_session.query(AuditEvent).filter_by(action="outage.complaint_linked").count()
        == 0
    )


def test_fresh_auth_runs_on_replay(db_session, reviewed, monkeypatch):
    outage.link_complaint_ticket(db_session, command=reviewed)

    def deny(*args, **kwargs):
        raise DomainError(
            code="auth.staff_permission_admission.denied", message="Permission revoked"
        )

    monkeypatch.setattr(outage, "revalidate_staff_admission", deny)
    with pytest.raises(DomainError, match="Permission revoked"):
        outage.link_complaint_ticket(db_session, command=reviewed)


def test_candidate_projection_uses_persisted_membership(
    db_session, reviewed, monkeypatch
):
    monkeypatch.setattr(
        outage,
        "_incident_scope_target",
        lambda *args: pytest.fail("Topology recomputed"),
    )
    result = outage.complaint_incident_candidates(
        db_session,
        query=ComplaintIncidentCandidatesQuery(
            ticket_id=reviewed.ticket_id,
            support_access=reviewed.support_access,
            monitoring_access=reviewed.monitoring_access,
        ),
    )
    assert len(result.rows) == 1
    assert result.rows[0].reviewed_revision_id == reviewed.expected_revision_id
    assert result.rows[0].readiness.is_ready


def test_read_only_staff_get_blocked_write_readiness(db_session, reviewed, monkeypatch):
    monkeypatch.setattr(outage, "has_permission", lambda *args, **kwargs: False)
    result = outage.complaint_incident_candidates(
        db_session,
        query=ComplaintIncidentCandidatesQuery(
            ticket_id=reviewed.ticket_id,
            support_access=reviewed.support_access,
            monitoring_access=reviewed.monitoring_access,
        ),
    )
    assert len(result.rows) == 1
    assert not result.rows[0].readiness.is_ready
    assert "permissions" in result.rows[0].readiness.blockers[0].staff_detail


def test_infrastructure_role_collision_is_refused(db_session, reviewed):
    db_session.add(
        OutageIncidentTicketLink(
            incident_id=reviewed.incident_id,
            ticket_id=reviewed.ticket_id,
            role="infrastructure",
            source="operator",
            scope_revision_sequence=1,
        )
    )
    db_session.commit()
    with pytest.raises(DomainError) as caught:
        outage.link_complaint_ticket(db_session, command=reviewed)
    assert caught.value.code == "outage.complaint.role_collision"
    assert db_session.query(OutageComplaintCorrelationReceipt).count() == 0


def test_native_receipt_codec_is_exact_and_fits_existing_column():
    from app.services.topology.outage_complaint_contracts import (
        ComplaintIncidentReceipt,
    )

    proof = ComplaintIncidentReceipt(
        fingerprint="a" * 64, link_id=uuid4(), subscription_id=uuid4()
    )
    encoded = proof.encode()
    assert len(encoded) == 113
    assert ComplaintIncidentReceipt.decode(encoded) == proof
    for invalid in (
        encoded.replace("v2:", "v1:", 1),
        encoded + "x",
        encoded.replace("a" * 64, "Z" * 64),
        "v2:a:bad:bad",
    ):
        with pytest.raises(ValueError):
            ComplaintIncidentReceipt.decode(invalid)


def test_historical_pair_without_native_selected_service_proof_refuses_new_intent(
    db_session, reviewed
):
    db_session.add(
        OutageIncidentTicketLink(
            incident_id=reviewed.incident_id,
            ticket_id=reviewed.ticket_id,
            role="complaint",
            source="operator",
            scope_revision_sequence=1,
        )
    )
    db_session.commit()
    with pytest.raises(DomainError) as caught:
        outage.link_complaint_ticket(db_session, command=reviewed)
    assert caught.value.code == "outage.complaint.provenance_unavailable"
    assert db_session.query(OutageComplaintCorrelationReceipt).count() == 0
    candidates = outage.complaint_incident_candidates(
        db_session,
        query=ComplaintIncidentCandidatesQuery(
            ticket_id=reviewed.ticket_id,
            support_access=reviewed.support_access,
            monitoring_access=reviewed.monitoring_access,
        ),
    )
    assert candidates.rows and candidates.rows[0].needs_legacy_review
    assert candidates.rows[0].readiness.is_ready


def test_existing_pair_cannot_claim_newly_joined_service_as_original(
    db_session, reviewed
):
    first = outage.link_complaint_ticket(db_session, command=reviewed)
    original = db_session.get(Subscription, reviewed.subscription_id)
    joined = Subscription(
        subscriber_id=original.subscriber_id, offer_id=original.offer_id
    )
    revision = OutageScopeRevision(
        incident_id=reviewed.incident_id,
        sequence=2,
        effective_at=outage.datetime.now(outage.UTC),
        reason="audience_drift",
        new_scope_type="unresolved",
        membership_token="b" * 64,
        member_count=2,
    )
    db_session.add_all([joined, revision])
    db_session.flush()
    db_session.add_all(
        [
            OutageScopeRevisionMember(
                revision_id=revision.id, subscription_id=joined.id, membership="entered"
            ),
            OutageScopeRevisionMember(
                revision_id=revision.id,
                subscription_id=original.id,
                membership="retained",
            ),
        ]
    )
    new_review = replace(
        reviewed,
        expected_revision_id=revision.id,
        expected_membership_token=revision.membership_token,
        subscription_id=joined.id,
        context=replace(reviewed.context, idempotency_key=str(uuid4())),
    )
    original_review = replace(new_review, subscription_id=original.id)
    db_session.commit()
    with pytest.raises(DomainError) as caught:
        outage.link_complaint_ticket(db_session, command=new_review)
    assert caught.value.code == "outage.complaint.provenance_conflict"
    candidates = outage.complaint_incident_candidates(
        db_session,
        query=ComplaintIncidentCandidatesQuery(
            ticket_id=reviewed.ticket_id,
            support_access=reviewed.support_access,
            monitoring_access=reviewed.monitoring_access,
        ),
    )
    by_service = {row.subscription_id: row for row in candidates.rows}
    assert not by_service[joined.id].readiness.is_ready
    assert by_service[original.id].readiness.is_ready
    db_session.rollback()
    outcome = outage.link_complaint_ticket(db_session, command=original_review)
    assert outcome.already_linked and outcome.link_id == first.link_id
    assert outcome.reviewed_revision_id == reviewed.expected_revision_id
    assert outcome.subscription_id == reviewed.subscription_id


def test_dual_permissions_from_different_staff_cannot_be_combined(
    db_session, reviewed, monkeypatch
):
    person_id = uuid4()

    def current_staff(db, *, admission, permission, write):
        return AuthorizedStaffPerson(admission.principal_id, person_id)

    monkeypatch.setattr(outage, "revalidate_staff_admission", current_staff)
    command = replace(
        reviewed,
        monitoring_access=replace(reviewed.monitoring_access, principal_id=uuid4()),
    )
    with pytest.raises(DomainError) as caught:
        outage.link_complaint_ticket(db_session, command=command)
    assert caught.value.code == "outage.complaint.denied"
    assert db_session.query(OutageIncidentTicketLink).count() == 0
