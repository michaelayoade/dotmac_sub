"""Real migrated outer-transaction provenance; coordinator executes PostgreSQL."""

# ruff: noqa: F811
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.network_monitoring import (
    NetworkDevice,
    OutageScopeRevision,
    OutageScopeRevisionMember,
)
from app.services.topology import affected, outage_reconcile
from tests.integration.test_outage_complaint_correlation_pg import command  # noqa: F401
from tests.integration.test_project_engineering_plan_acceptance import (
    native_engine as engine,  # noqa: F401
)


def _revision(db, command):
    row = OutageScopeRevision(
        incident_id=command.incident_id,
        sequence=2,
        effective_at=datetime.now(UTC),
        reason="audience_drift",
        new_scope_type="unresolved",
        membership_token="a" * 64,
        member_count=1,
        creation_outer_xid="1",
    )
    db.add(row)
    db.flush()
    db.refresh(row, attribute_names=["creation_outer_xid"])
    assert row.creation_outer_xid == str(db.scalar(text("SELECT pg_current_xact_id()")))
    assert row.creation_outer_xid != "1"
    return row


def _member(db, command, revision_id):
    db.add(
        OutageScopeRevisionMember(
            revision_id=revision_id,
            subscription_id=command.subscription_id,
            membership="retained",
        )
    )
    db.flush()


@pytest.mark.parametrize("mode", ["inside", "released", "sibling"])
def test_initial_audience_accepts_exact_outer_transaction_across_savepoints(
    engine, command, mode
):
    with Session(engine) as db:
        with db.begin_nested():
            row = _revision(db, command)
            revision_id = row.id
            if mode == "inside":
                _member(db, command, revision_id)
        if mode == "released":
            _member(db, command, revision_id)
        if mode == "sibling":
            with db.begin_nested():
                _member(db, command, revision_id)
        db.commit()
    with Session(engine) as db:
        assert (
            db.scalar(
                select(OutageScopeRevisionMember).where(
                    OutageScopeRevisionMember.revision_id == revision_id
                )
            )
            is not None
        )


def test_committed_parent_rejects_late_member_and_marker_rewrite(engine, command):
    with Session(engine) as db:
        row = _revision(db, command)
        revision_id = row.id
        db.commit()
    with Session(engine) as db:
        with pytest.raises(DBAPIError, match="initial revision"):
            _member(db, command, revision_id)
        db.rollback()
        with pytest.raises(DBAPIError):
            db.execute(
                text(
                    "UPDATE outage_scope_revisions SET creation_outer_xid=pg_current_xact_id() WHERE id=:id"
                ),
                {"id": revision_id},
            )
        db.rollback()


def test_mutable_link_marker_cannot_be_rewritten(engine, command):
    from tests.integration.test_shared_outage_completion_acceptance import (
        _legacy_receipt_fixture,
    )

    link, _, _ = _legacy_receipt_fixture(engine, command)
    with Session(engine) as db:
        with pytest.raises(
            DBAPIError, match="outer transaction provenance is immutable"
        ):
            db.execute(
                text(
                    "UPDATE outage_incident_ticket_links SET creation_outer_xid=pg_current_xact_id() WHERE id=:id"
                ),
                {"id": link},
            )
        db.rollback()


def test_real_classifier_reconcile_creates_scope_members_inside_its_savepoint(
    engine, command, monkeypatch
):
    with Session(engine) as db:
        node = NetworkDevice(name="Synthetic classifier outer transaction regression")
        db.add(node)
        db.commit()
        node_id = node.id
        db.rollback()
    with Session(engine) as db:
        node = db.get(NetworkDevice, node_id)
        service = db.get(Subscription, command.subscription_id)
        candidate = outage_reconcile._Candidate(
            root_node=node,
            basestation_id=None,
            affected_count=1,
            confidence=0.9,
            classification="network_dark",
            component_node_ids={node_id},
        )
        monkeypatch.setattr(
            outage_reconcile,
            "_candidate_outages",
            lambda session, now: {node_id: candidate},
        )
        monkeypatch.setattr(
            affected,
            "affected_customers",
            lambda session, **kwargs: {"subscriptions": [service]},
        )
        counters = outage_reconcile.reconcile_detected_outages(db)
        assert counters["suspected_opened"] == 1 and counters["errors"] == 0
        db.commit()
        assert (
            db.scalar(
                select(OutageScopeRevisionMember)
                .join(OutageScopeRevision)
                .where(OutageScopeRevision.new_scope_id == node_id)
            )
            is not None
        )


def test_connection_bound_owner_request_stages_children_in_exact_outer_transaction(
    engine, command
):

    from tests.integration.test_shared_outage_completion_acceptance import setup_shared

    setup_shared(engine, command)
    _request_in_external_connection(engine, command)


def _request_in_external_connection(engine, command):
    from uuid import uuid4

    from app.services import support
    from app.services.owner_commands import CommandContext
    from app.services.support_resolution_contracts import (
        SupportResolutionRequestCommand,
    )
    from app.services.topology import outage_completion
    from app.services.topology.outage_completion_contracts import (
        ComplaintResolutionScopesPreviewQuery,
        EmptySharedScopeReview,
        ReviewedComplaintResolutionScope,
    )

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
                tuple(issue.issuance_link_id for issue in x.issuances),
                EmptySharedScopeReview.restored_without_issued_field_scope
                if not x.issuances
                else None,
            )
            for x in scopes
        )
    with engine.connect() as connection:
        outer = connection.begin()
        try:
            with Session(connection, join_transaction_mode="create_savepoint") as db:
                result = support.request_resolution(
                    db,
                    command=SupportResolutionRequestCommand(
                        ticket_id=command.ticket_id,
                        actor_id=command.support_access.principal_id,
                        access=command.support_access,
                        grace_hours=24,
                        context=CommandContext.system(
                            actor=str(command.support_access.principal_id),
                            scope="support.ticket:request_resolution_confirmation",
                            reason="Connection owner savepoint provenance regression",
                            idempotency_key=str(uuid4()),
                        ),
                        reviewed_shared_scopes=reviewed,
                    ),
                )
                assert (
                    db.scalar(
                        text(
                            "SELECT creation_outer_xid=pg_current_xact_id() FROM support_resolution_requests WHERE id=:id"
                        ),
                        {"id": result.request_id},
                    )
                    is True
                )
            outer.commit()
        finally:
            if outer.is_active:
                outer.rollback()


def test_external_connection_reviewed_replacement_and_effective_support_basis_physically_commit(
    engine, command
):
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
    with engine.connect() as connection:
        outer = connection.begin()
        try:
            with Session(connection, join_transaction_mode="create_savepoint") as db:
                outcome = completion.review_failed_scope_replacement(
                    db, command=reviewed
                )
            outer.commit()
        finally:
            if outer.is_active:
                outer.rollback()
    with Session(engine) as db:
        assert (
            db.scalar(
                text(
                    "SELECT count(*) FROM completion_replacement_coverage WHERE decision_id=:id"
                ),
                {"id": outcome.decision_id},
            )
            > 0
        )
        assert (
            db.scalar(
                text(
                    "SELECT count(*) FROM completion_replacement_heads WHERE decision_id=:id"
                ),
                {"id": outcome.decision_id},
            )
            == 1
        )
    accept_enrolled(engine, replacement)
    restore(engine, command)
    _request_in_external_connection(engine, command)
    with Session(engine) as db:
        assert (
            db.scalar(
                text(
                    "SELECT count(*) FROM support_resolution_replacement_lineage lineage JOIN support_resolution_requests request ON request.id=lineage.request_id WHERE request.ticket_id=:id"
                ),
                {"id": command.ticket_id},
            )
            > 0
        )
        assert (
            db.scalar(
                text(
                    "SELECT count(*) FROM support_resolution_effective_bases basis JOIN support_resolution_requests request ON request.id=basis.request_id WHERE request.ticket_id=:id"
                ),
                {"id": command.ticket_id},
            )
            > 0
        )
