"""681 migrated PostgreSQL acceptance, authored for coordinator-only execution."""

# ruff: noqa: F811
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.models.network_monitoring import (
    OutageIncident,
    OutageIncidentTicketLink,
    OutageIncidentWorkOrderLink,
)
from app.models.outage_complaint_correlation import OutageComplaintCorrelationReceipt
from app.models.shared_outage_completion import SupportResolutionSharedIncident
from app.models.support import Ticket
from app.models.work_order import WorkOrder
from app.services import support
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.support_resolution_contracts import SupportResolutionRequestCommand
from app.services.topology import outage, outage_completion
from app.services.topology.outage_completion_contracts import (
    ComplaintResolutionScopesPreviewQuery,
    EmptySharedScopeReview,
    ReviewedComplaintResolutionScope,
)
from tests.integration.test_outage_complaint_correlation_pg import command  # noqa: F401


def setup_shared(engine, command, *, second_complaint=False):
    with Session(engine) as db:
        outcome = outage.link_complaint_ticket(db, command=command)
    second = None
    if second_complaint:
        with Session(engine) as db:
            customer = db.get(Ticket, command.ticket_id).subscriber_id
            ticket = Ticket(
                title="Second independently reviewed complaint",
                subscriber_id=customer,
                status="open",
            )
            db.add(ticket)
            db.commit()
            second = replace(
                command,
                ticket_id=ticket.id,
                context=replace(command.context, idempotency_key=str(uuid4())),
            )
        with Session(engine) as db:
            outage.link_complaint_ticket(db, command=second)
    with Session(engine) as db:
        infra = Ticket(title="Shared network repair", status="open")
        db.add(infra)
        db.flush()
        db.add(
            OutageIncidentTicketLink(
                incident_id=command.incident_id,
                ticket_id=infra.id,
                role="infrastructure",
                source="operator",
            )
        )
        row = db.get(OutageIncident, command.incident_id)
        row.status = "resolved"
        row.resolved_at = datetime.now(UTC)
        db.commit()
        return outcome, infra.id, second


def request(engine, command):
    with Session(engine) as db:
        scopes = outage_completion.preview_complaint_resolution_scopes(
            db,
            query=ComplaintResolutionScopesPreviewQuery(
                command.ticket_id, command.support_access
            ),
        ).incidents
        reviewed = tuple(
            ReviewedComplaintResolutionScope(
                x.complaint_link_id,
                x.reviewed_revision_id,
                x.membership_token,
                x.resolved_at,
                tuple(y.issuance_link_id for y in x.issuances),
                EmptySharedScopeReview.restored_without_issued_field_scope
                if not x.issuances
                else None,
            )
            for x in scopes
        )
    with Session(engine) as db:
        return support.request_resolution(
            db,
            command=SupportResolutionRequestCommand(
                ticket_id=command.ticket_id,
                actor_id=command.support_access.principal_id,
                access=command.support_access,
                grace_hours=24,
                context=CommandContext.system(
                    actor=str(command.support_access.principal_id),
                    scope="support.ticket:request_resolution_confirmation",
                    reason="Reviewed shared restoration and every actual issued field scope",
                    idempotency_key=str(uuid4()),
                ),
                reviewed_shared_scopes=reviewed,
            ),
        )


def confirm(engine, command):
    with Session(engine) as db:
        row = db.get(Ticket, command.ticket_id)
        subscriber = row.subscriber_id
        db.commit()
        return support.Tickets.respond_to_resolution_for_customer(
            db, row, confirm=True, expected_subscriber_ids=(subscriber,)
        )


def test_two_complaints_confirm_independently_without_shared_customer_closure(
    engine, command
):
    _, _, second = setup_shared(engine, command, second_complaint=True)
    first = request(engine, command)
    other = request(engine, second)
    assert first.request_id != other.request_id
    confirm(engine, command)
    with Session(engine) as db:
        assert db.get(Ticket, command.ticket_id).status == "closed"
        assert db.get(Ticket, second.ticket_id).status == "pending_confirmation"
    confirm(engine, second)


@pytest.mark.parametrize("drift", ["reopen", "re_resolve"])
def test_restoration_change_waits_then_refuses_confirm(engine, command, drift):
    setup_shared(engine, command)
    request(engine, command)
    started = Event()
    with Session(engine) as writer:
        row = writer.scalar(
            select(OutageIncident)
            .where(OutageIncident.id == command.incident_id)
            .with_for_update()
        )
        if drift == "reopen":
            row.status = "open"
        else:
            row.resolved_at = datetime.now(UTC)
        writer.flush()

        def run():
            started.set()
            with pytest.raises(DomainError):
                confirm(engine, command)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run)
            assert started.wait(5)
            writer.commit()
            future.result(timeout=15)
    with Session(engine) as db:
        assert db.get(Ticket, command.ticket_id).status == "pending_confirmation"


def test_native_receipt_is_immutable_and_has_no_generic_legacy_authority(
    engine, command
):
    with Session(engine) as db:
        outage.link_complaint_ticket(db, command=command)
    with Session(engine) as db:
        row = db.scalar(
            select(OutageComplaintCorrelationReceipt).where(
                OutageComplaintCorrelationReceipt.incident_id == command.incident_id
            )
        )
        assert row.subscription_id == command.subscription_id
        with pytest.raises(DBAPIError):
            db.execute(
                text(
                    "UPDATE outage_complaint_correlation_receipts SET reason='fabricated' WHERE id=:id"
                ),
                {"id": row.id},
            )
            db.commit()
        db.rollback()


def test_committed_request_cannot_receive_late_shared_incident_child(engine, command):
    setup_shared(engine, command)
    result = request(engine, command)
    with Session(engine) as db:
        row = db.scalar(
            select(SupportResolutionSharedIncident).where(
                SupportResolutionSharedIncident.request_id == result.request_id
            )
        )
        # Even a duplicate exact child must hit creation guard before unique/FK;
        # direct SQL cannot attach new evidence to historical reviewed authority.
        columns = [x.name for x in row.__table__.columns]
        statement = (
            "INSERT INTO support_resolution_shared_incidents ("  # noqa: S608 -- fixed native model columns
            + ",".join(columns)
            + ") SELECT "
            + ",".join(columns)
            + " FROM support_resolution_shared_incidents WHERE request_id=:id"
        )
        with pytest.raises(
            DBAPIError, match="created with reviewed resolution request"
        ):
            db.execute(text(statement), {"id": result.request_id})
            db.commit()
        db.rollback()


def test_actual_unenrolled_scope_blocks_shared_confirmation_request(engine, command):
    # Issue while active, then restore independently. The failed/unenrolled job
    # remains required; restoration cannot manufacture accepted completion.
    with Session(engine) as db:
        outage.link_complaint_ticket(db, command=command)
    with Session(engine) as db:
        infra = Ticket(title="Infrastructure scope", status="open")
        db.add(infra)
        db.flush()
        db.add(
            OutageIncidentTicketLink(
                incident_id=command.incident_id,
                ticket_id=infra.id,
                role="infrastructure",
                source="operator",
            )
        )
        db.flush()
        work = WorkOrder(
            title="Unenrolled issued repair",
            origin_ticket_id=infra.id,
            subscriber_id=None,
            work_order_kind="infrastructure",
            purpose="repair",
            access_technology="fiber",
            template_version=1,
            classification_source="explicit_operator_selection",
        )
        db.add(work)
        db.flush()
        db.add(
            OutageIncidentWorkOrderLink(
                incident_id=command.incident_id,
                work_order_id=work.id,
                scope_revision_id=command.expected_revision_id,
                scope_revision_sequence=1,
                membership_token=command.expected_membership_token,
                target_type="unresolved",
                idempotency_key=str(uuid4()),
                command_fingerprint="a" * 64,
                created_by=str(command.support_access.principal_id),
            )
        )
        db.commit()
    with Session(engine) as db:
        row = db.get(OutageIncident, command.incident_id)
        row.status = "resolved"
        row.resolved_at = datetime.now(UTC)
        db.commit()
    with pytest.raises(DomainError, match="enrollment"):
        request(engine, command)


def test_actual_accepted_infrastructure_job_closes_two_reviewed_complaints_independently(
    engine, command
):
    from tests.integration.test_support_completion_gate_acceptance import accept
    from tests.shared_outage_fixtures import (
        active_shared,
        issue_actual,
        prepare_issued_job,
        restore,
    )

    active_shared(engine, command)
    with Session(engine) as db:
        original = db.get(Ticket, command.ticket_id)
        second = Ticket(
            title="Second customer report for one actual shared visit",
            subscriber_id=original.subscriber_id,
            status="open",
        )
        db.add(second)
        db.commit()
        second_command = replace(
            command,
            ticket_id=second.id,
            expected_revision_id=outage.latest_scope_revision(
                db, command.incident_id
            ).id,
            expected_membership_token="b" * 64,
            context=replace(command.context, idempotency_key=str(uuid4())),
        )
        db.commit()
    with Session(engine) as db:
        outage.link_complaint_ticket(db, command=second_command)
    job = prepare_issued_job(engine, work_order_id=issue_actual(engine, command))
    accept(engine, job)
    restore(engine, command)
    request(engine, command)
    request(engine, second_command)
    confirm(engine, command)
    with Session(engine) as db:
        assert db.get(Ticket, command.ticket_id).status == "closed"
        assert db.get(Ticket, second_command.ticket_id).status == "pending_confirmation"
    confirm(engine, second_command)


def test_reviewed_failed_shared_scope_replacement_with_audience_growth(engine, command):
    from app.models.shared_outage_completion import CompletionReplacementOutageProof
    from app.services.field import completion_review as completion
    from tests.integration.test_failed_scope_replacement_acceptance import (
        accept_enrolled,
        command_for,
        enroll,
        fail,
    )
    from tests.shared_outage_fixtures import (
        active_shared,
        issue_actual,
        next_audience,
        prepare_issued_job,
        restore,
    )

    active_shared(engine, command)
    source = prepare_issued_job(engine, work_order_id=issue_actual(engine, command))
    fail(engine, source)
    enroll(engine, source)
    next_audience(engine, command)
    replacement = prepare_issued_job(
        engine, work_order_id=issue_actual(engine, command)
    )
    enroll(engine, replacement)
    reviewed = command_for(engine, source, replacement)
    with Session(engine) as db:
        outcome = completion.review_failed_scope_replacement(db, command=reviewed)
    accept_enrolled(engine, replacement)
    restore(engine, command)
    result = request(engine, command)
    with Session(engine) as db:
        proof = db.get(CompletionReplacementOutageProof, outcome.decision_id)
        assert proof.revision_id != proof.replacement_revision_id
        assert db.get(Ticket, command.ticket_id).status == "pending_confirmation"
    confirm(engine, command)
    assert result.request_id is not None


@pytest.mark.parametrize(
    "drift", ["omit_original_service", "same_count_different_member", "target_changed"]
)
def test_replacement_issued_audience_or_target_drift_refuses_before_review(
    engine, command, drift
):
    from app.services.field import completion_review as completion
    from app.services.field.completion_review_contracts import (
        FailedScopeReplacementWorkspaceQuery,
        ReplacementWorkspacePurpose,
    )
    from tests.integration.test_failed_scope_replacement_acceptance import enroll, fail
    from tests.shared_outage_fixtures import (
        active_shared,
        issue_actual,
        next_audience,
        prepare_issued_job,
    )

    active_shared(engine, command)
    source = prepare_issued_job(engine, work_order_id=issue_actual(engine, command))
    fail(engine, source)
    enroll(engine, source)
    # A distinct persisted service replaces the original for both omission
    # cases; equal count is deliberately not accepted as identity coverage.
    next_audience(
        engine,
        command,
        retain_source=drift == "target_changed",
        new_target=drift == "target_changed",
    )
    replacement = prepare_issued_job(
        engine, work_order_id=issue_actual(engine, command)
    )
    enroll(engine, replacement)
    with Session(engine) as db:
        workspace = completion.failed_scope_replacement_workspace(
            db,
            query=FailedScopeReplacementWorkspaceQuery(
                access=source.access,
                work_order_public_id=source.public_id,
                purpose=ReplacementWorkspacePurpose.review,
            ),
        )
        assert not any(
            x.work_order_public_id == replacement.public_id
            for x in workspace.candidates
        )


def test_new_issued_scope_waits_then_invalidates_existing_confirmation(
    engine, command, monkeypatch
):
    from app.services.topology import outage_work_order_handoff as issuer
    from tests.shared_outage_fixtures import active_shared, issue_actual, restore

    active_shared(engine, command)
    restore(engine, command)
    request(engine, command)
    staged, release, confirming = Event(), Event(), Event()
    original = issuer._issue_work_order

    def held(db, incoming):
        incident = db.scalar(
            select(OutageIncident)
            .where(OutageIncident.id == command.incident_id)
            .with_for_update()
        )
        original_restoration = incident.resolved_at
        incident.status = "confirmed"
        outcome = original(db, incoming)
        # One transaction exposes new actual issuance, keeping restoration
        # unchanged to isolate scope-set drift from restoration-time drift.
        incident.status = "resolved"
        incident.resolved_at = original_restoration
        db.flush()
        staged.set()
        assert release.wait(10)
        return outcome

    monkeypatch.setattr(issuer, "_issue_work_order", held)

    def response():
        confirming.set()
        with pytest.raises(DomainError):
            confirm(engine, command)

    with ThreadPoolExecutor(max_workers=2) as pool:
        issued = pool.submit(issue_actual, engine, command)
        assert staged.wait(10)
        customer = pool.submit(response)
        assert confirming.wait(5)
        release.set()
        issued.result(timeout=15)
        customer.result(timeout=15)


def test_new_correlation_waits_then_invalidates_existing_request(
    engine, command, monkeypatch
):
    # A second exact incident is added while the ticket is pending. No copied
    # original request can silently cover newly reviewed shared scope.
    setup_shared(engine, command)
    request(engine, command)
    from app.models.network_monitoring import (
        OutageScopeRevision,
        OutageScopeRevisionMember,
    )

    with Session(engine) as db:
        other = OutageIncident(status="confirmed", note="Second shared incident")
        infra = Ticket(title="Second canonical shared repair", status="open")
        db.add_all([other, infra])
        db.flush()
        revision = OutageScopeRevision(
            incident_id=other.id,
            sequence=1,
            effective_at=datetime.now(UTC),
            reason="confirmed",
            new_scope_type="unresolved",
            membership_token="c" * 64,
            member_count=1,
        )
        db.add(revision)
        db.flush()
        db.add(
            OutageScopeRevisionMember(
                revision_id=revision.id,
                subscription_id=command.subscription_id,
                membership="entered",
            )
        )
        db.add(
            OutageIncidentTicketLink(
                incident_id=other.id,
                ticket_id=infra.id,
                role="infrastructure",
                source="operator",
            )
        )
        db.commit()
        other_command = replace(
            command,
            incident_id=other.id,
            expected_revision_id=revision.id,
            expected_membership_token=revision.membership_token,
            context=replace(command.context, idempotency_key=str(uuid4())),
        )
    staged, release, confirming = Event(), Event(), Event()
    original = outage._stage_complaint_ticket

    def held(db, *, command):
        result = original(db, command=command)
        staged.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(outage, "_stage_complaint_ticket", held)

    def link():
        with Session(engine) as db:
            return outage.link_complaint_ticket(db, command=other_command)

    def response():
        confirming.set()
        with pytest.raises(DomainError):
            confirm(engine, command)

    with ThreadPoolExecutor(max_workers=2) as pool:
        linked = pool.submit(link)
        assert staged.wait(10)
        customer = pool.submit(response)
        assert confirming.wait(5)
        release.set()
        linked.result(timeout=15)
        customer.result(timeout=15)


def _legacy_receipt_fixture(engine, command):
    from app.models.catalog import Subscription
    from app.models.network_monitoring import (
        OutageScopeRevision,
        OutageScopeRevisionMember,
    )

    with Session(engine) as db:
        link = OutageIncidentTicketLink(
            incident_id=command.incident_id,
            ticket_id=command.ticket_id,
            role="complaint",
            source="operator",
            linked_by="historical",
        )
        service = db.get(Subscription, command.subscription_id)
        other = Subscription(
            subscriber_id=service.subscriber_id, offer_id=service.offer_id
        )
        db.add_all([link, other])
        db.flush()
        revision = OutageScopeRevision(
            incident_id=command.incident_id,
            sequence=2,
            effective_at=datetime.now(UTC),
            reason="audience_drift",
            new_scope_type="unresolved",
            membership_token="d" * 64,
            member_count=2,
        )
        db.add(revision)
        db.flush()
        db.add_all(
            [
                OutageScopeRevisionMember(
                    revision_id=revision.id, subscription_id=x, membership="retained"
                )
                for x in (service.id, other.id)
            ]
        )
        db.commit()
        return link.id, revision.id, other.id


def _raw_receipt(command, link_id, revision_id, subscription_id):
    from app.services.topology.outage_complaint_contracts import (
        ComplaintIncidentReceipt,
    )

    fingerprint = uuid4().hex * 2
    return OutageComplaintCorrelationReceipt(
        incident_id=command.incident_id,
        intent_id=uuid4(),
        fingerprint=fingerprint,
        link_id=link_id,
        subscription_id=subscription_id,
        original_revision_id=revision_id,
        actor_user_id=command.support_access.principal_id,
        reason="Explicit synthetic historical correlation review",
        proof_origin="reviewed_adoption",
        ref_id=ComplaintIncidentReceipt(fingerprint, link_id, subscription_id).encode(),
    )


def test_raw_concurrent_native_adoptions_cannot_bind_two_original_services(
    engine, command
):
    from threading import Barrier

    link, revision, other = _legacy_receipt_fixture(engine, command)
    ready = Barrier(2)

    def insert(subscription):
        with Session(engine) as db:
            db.add(_raw_receipt(command, link, revision, subscription))
            ready.wait(timeout=10)
            try:
                db.commit()
                return True
            except DBAPIError:
                db.rollback()
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(insert, (command.subscription_id, other)))
    assert sorted(results) == [False, True]
    with Session(engine) as db:
        assert (
            len(
                db.scalars(
                    select(OutageComplaintCorrelationReceipt).where(
                        OutageComplaintCorrelationReceipt.link_id == link
                    )
                ).all()
            )
            == 1
        )


def test_raw_adoption_cannot_elect_old_revision_or_change_native_origin(
    engine, command
):
    link, current, _ = _legacy_receipt_fixture(engine, command)
    with Session(engine) as db:
        db.add(
            _raw_receipt(
                command, link, command.expected_revision_id, command.subscription_id
            )
        )
        with pytest.raises(DBAPIError, match="exact current reviewed audience"):
            db.commit()
        db.rollback()
    with Session(engine) as db:
        db.add(_raw_receipt(command, link, current, command.subscription_id))
        db.commit()
    with Session(engine) as db:
        receipt = _raw_receipt(
            command, link, command.expected_revision_id, command.subscription_id
        )
        db.add(receipt)
        with pytest.raises(
            DBAPIError, match="original service revision and origin are immutable"
        ):
            db.commit()
        db.rollback()


def test_native_receipt_timestamp_cannot_backdate_original_authority(engine, command):
    from datetime import timedelta

    link, current, _ = _legacy_receipt_fixture(engine, command)
    with Session(engine) as db:
        receipt = _raw_receipt(command, link, current, command.subscription_id)
        receipt.created_at = datetime.now(UTC) - timedelta(days=365)
        db.add(receipt)
        db.commit()
        db.refresh(receipt)
        assert receipt.created_at > datetime.now(UTC) - timedelta(minutes=1)


@pytest.mark.parametrize("owner_kind", ["absent", "mismatched"])
def test_raw_adoption_requires_positive_exact_native_customer_binding(
    engine, command, owner_kind
):
    from app.models.catalog import Subscription
    from app.models.subscriber import Subscriber

    link, current, _ = _legacy_receipt_fixture(engine, command)
    with Session(engine) as db:
        ticket = db.get(Ticket, command.ticket_id)
        ticket.subscriber_id = None
        ticket.customer_account_id = None
        ticket.customer_person_id = None
        if owner_kind == "mismatched":
            original = db.get(
                Subscriber, db.get(Subscription, command.subscription_id).subscriber_id
            )
            other = Subscriber(
                reseller_id=original.reseller_id,
                first_name="Other",
                last_name="Customer",
                email=f"{uuid4()}@example.test",
            )
            db.add(other)
            db.flush()
            ticket.subscriber_id = other.id
        db.commit()
    with Session(engine) as db:
        db.add(_raw_receipt(command, link, current, command.subscription_id))
        with pytest.raises(
            DBAPIError, match="exact native complaint customer service binding"
        ):
            db.commit()
        db.rollback()


@pytest.mark.parametrize("invalid_state", ["old_revision", "resolved"])
def test_raw_first_new_link_receipt_requires_current_open_incident(
    engine, command, invalid_state
):
    if invalid_state == "old_revision":
        _legacy_receipt_fixture(engine, command)
    with Session(engine) as db:
        original = db.get(Ticket, command.ticket_id)
        ticket = Ticket(
            title="Explicit new complaint", subscriber_id=original.subscriber_id
        )
        db.add(ticket)
        db.flush()
        if invalid_state == "resolved":
            incident = db.get(OutageIncident, command.incident_id)
            incident.status = "resolved"
            incident.resolved_at = datetime.now(UTC)
        link = OutageIncidentTicketLink(
            incident_id=command.incident_id,
            ticket_id=ticket.id,
            role="complaint",
            source="operator",
            linked_by=str(command.support_access.principal_id),
            scope_revision_sequence=1,
        )
        db.add(link)
        db.flush()
        receipt = _raw_receipt(
            command, link.id, command.expected_revision_id, command.subscription_id
        )
        receipt.proof_origin = "new_link"
        db.add(receipt)
        with pytest.raises(DBAPIError, match="first new link receipt requires"):
            db.commit()
        db.rollback()
