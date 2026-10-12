"""Migrated PostgreSQL locking and fresh staff authorization acceptance.

Run only through the repository's disposable migrated PostgreSQL lane.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import (
    AccessType,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
)
from app.models.network_monitoring import (
    OutageIncident,
    OutageIncidentTicketLink,
    OutageScopeRevision,
    OutageScopeRevisionMember,
)
from app.models.outage_complaint_correlation import OutageComplaintCorrelationReceipt
from app.models.rbac import Permission, SystemUserPermission
from app.models.subscriber import Reseller, Subscriber
from app.models.support import Ticket
from app.models.system_user import SystemUser
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.topology import outage
from app.services.topology.outage_complaint_contracts import (
    MONITORING_READ,
    MONITORING_WRITE,
    SUPPORT_READ,
    SUPPORT_WRITE,
    ComplaintIncidentLinkCommand,
)
from tests.staff_identity_fixtures import add_bound_staff_user


@pytest.fixture
def command(engine):
    assert engine.dialect.name == "postgresql"
    with Session(engine, expire_on_commit=False) as db:
        staff, _ = add_bound_staff_user(db)
        keys = {SUPPORT_READ, SUPPORT_WRITE, MONITORING_READ, MONITORING_WRITE}
        # Runtime RBAC seeding supplies these existing catalogue keys. The real
        # Alembic chain does not seed every legacy write permission on an empty DB.
        permissions = list(
            db.scalars(select(Permission).where(Permission.key.in_(keys))).all()
        )
        for key in sorted(keys - {row.key for row in permissions}):
            permission = Permission(
                key=key, description="Synthetic explicit staff grant", is_active=True
            )
            db.add(permission)
            permissions.append(permission)
        db.flush()
        assert {row.key for row in permissions if row.is_active} == keys
        for permission in permissions:
            db.add(
                SystemUserPermission(
                    system_user_id=staff.id, permission_id=permission.id
                )
            )
        reseller = Reseller(
            name="Synthetic correlation reseller",
            code=f"qa-{uuid4().hex}",
            is_active=True,
        )
        db.add(reseller)
        db.flush()
        subscriber = Subscriber(
            reseller_id=reseller.id,
            first_name="Synthetic",
            last_name="Incident complaint",
            email=f"{uuid4()}@example.test",
        )
        offer = CatalogOffer(
            name=f"Complaint review {uuid4()}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        )
        db.add_all([subscriber, offer])
        db.flush()
        subscription = Subscription(
            subscriber_id=subscriber.id,
            offer_id=offer.id,
            service_description="Synthetic feeder service",
        )
        incident = OutageIncident(
            status="confirmed", note="Synthetic feeder interruption"
        )
        ticket = Ticket(
            title="Synthetic subscriber complaint",
            subscriber_id=subscriber.id,
            status="open",
        )
        db.add_all([subscription, incident, ticket])
        db.flush()
        revision = OutageScopeRevision(
            incident_id=incident.id,
            sequence=1,
            effective_at=datetime.now(UTC),
            reason="confirmed",
            new_scope_type="unresolved",
            membership_token="a" * 64,
            member_count=1,
        )
        db.add(revision)
        db.flush()
        db.add(
            OutageScopeRevisionMember(
                revision_id=revision.id,
                subscription_id=subscription.id,
                membership="entered",
            )
        )
        access = PermissionAdmission(staff.id, "system_user", frozenset(keys))
        reason = "Synthetic migrated incident complaint review"
        result = ComplaintIncidentLinkCommand(
            context=CommandContext.system(
                actor=str(staff.id),
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
        db.commit()
        return result


@pytest.mark.parametrize("same_intent", [True, False])
def test_concurrent_reviewed_pair_serializes(engine, command, same_intent):
    barrier = Barrier(2)
    other = (
        command
        if same_intent
        else replace(
            command, context=replace(command.context, idempotency_key=str(uuid4()))
        )
    )

    def apply(value):
        with Session(engine) as db:
            barrier.wait(timeout=20)
            return outage.link_complaint_ticket(db, command=value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(apply, value) for value in (command, other)]
        results = [future.result(timeout=40) for future in futures]
    assert results[0].link_id == results[1].link_id
    assert sum(result.replayed for result in results) == int(same_intent)
    with Session(engine) as db:
        links = db.scalars(
            select(OutageIncidentTicketLink).where(
                OutageIncidentTicketLink.incident_id == command.incident_id
            )
        ).all()
        receipts = db.scalars(
            select(OutageComplaintCorrelationReceipt).where(
                OutageComplaintCorrelationReceipt.incident_id == command.incident_id
            )
        ).all()
        assert len(links) == 1
        assert len(receipts) == (1 if same_intent else 2)


def test_replay_refuses_current_inactive_staff(engine, command):
    with Session(engine) as db:
        outage.link_complaint_ticket(db, command=command)
    with Session(engine) as db:
        db.get(SystemUser, command.support_access.principal_id).is_active = False
        db.commit()
    with Session(engine) as db, pytest.raises(DomainError) as caught:
        outage.link_complaint_ticket(db, command=command)
    assert caught.value.code == "auth.staff_permission_admission.denied"


def test_committed_revision_change_refuses_original_review(engine, command):
    with Session(engine) as db:
        incident = db.scalar(
            select(OutageIncident)
            .where(OutageIncident.id == command.incident_id)
            .with_for_update()
        )
        db.add(
            OutageScopeRevision(
                incident_id=incident.id,
                sequence=2,
                effective_at=datetime.now(UTC),
                reason="audience_drift",
                new_scope_type="unresolved",
                membership_token="b" * 64,
                member_count=0,
            )
        )
        db.commit()
    with Session(engine) as db, pytest.raises(DomainError) as caught:
        outage.link_complaint_ticket(db, command=command)
    assert caught.value.code == "outage.complaint.stale_revision"


def test_scope_revision_writer_and_link_share_incident_lock(engine, command):
    from app.services.topology.outage_complaint_contracts import (
        RecordOutageScopeRevision,
    )

    gate = Barrier(2)

    def drift():
        with Session(engine) as db:
            gate.wait(timeout=20)
            recorded = outage.record_scope_revision(
                db,
                command=RecordOutageScopeRevision(
                    incident_id=command.incident_id, reason="audience_drift"
                ),
            )
            db.commit()
            return recorded

    def link():
        with Session(engine) as db:
            gate.wait(timeout=20)
            try:
                return outage.link_complaint_ticket(db, command=command)
            except DomainError as exc:
                assert exc.code == "outage.complaint.stale_revision"
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        revision_future = pool.submit(drift)
        link_future = pool.submit(link)
        recorded = revision_future.result(timeout=40)
        linked = link_future.result(timeout=40)
    assert recorded.sequence == 2
    with Session(engine) as db:
        revisions = db.scalars(
            select(OutageScopeRevision)
            .where(OutageScopeRevision.incident_id == command.incident_id)
            .order_by(OutageScopeRevision.sequence)
        ).all()
        assert [row.sequence for row in revisions] == [1, 2]
        if linked is not None:
            assert linked.reviewed_revision_sequence == 1
        else:
            assert (
                db.scalar(
                    select(OutageIncidentTicketLink).where(
                        OutageIncidentTicketLink.incident_id == command.incident_id
                    )
                )
                is None
            )


def test_selected_subscription_binding_is_rechecked_after_lock_wait(engine, command):
    from threading import Event, current_thread

    from sqlalchemy import event

    reached_subscription_lock = Event()

    def observe(connection, cursor, statement, parameters, context, executemany):
        if (
            "subscriptions" in statement
            and "FOR UPDATE" in statement
            and current_thread().name.startswith("complaint-binding")
        ):
            reached_subscription_lock.set()

    def apply():
        with Session(engine) as db:
            try:
                return outage.link_complaint_ticket(db, command=command)
            except DomainError as exc:
                return exc.code

    with Session(engine) as changing:
        subscription = changing.scalar(
            select(Subscription)
            .where(Subscription.id == command.subscription_id)
            .with_for_update()
        )
        original_customer = changing.get(Subscriber, subscription.subscriber_id)
        assert original_customer is not None
        other = Subscriber(
            reseller_id=original_customer.reseller_id,
            first_name="Other",
            last_name="Synthetic customer",
            email=f"{uuid4()}@example.test",
        )
        changing.add(other)
        changing.flush()
        event.listen(engine, "before_cursor_execute", observe)
        try:
            with ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="complaint-binding"
            ) as pool:
                future = pool.submit(apply)
                try:
                    assert reached_subscription_lock.wait(timeout=20), (
                        "Complaint command did not reach selected Subscription lock"
                    )
                    subscription.subscriber_id = other.id
                    changing.commit()
                finally:
                    # Release the held row on a failed synchronization assertion too.
                    if changing.in_transaction():
                        changing.rollback()
                assert future.result(timeout=30) == "outage.complaint.scope_mismatch"
        finally:
            event.remove(engine, "before_cursor_execute", observe)
    with Session(engine) as db:
        assert (
            db.scalar(
                select(OutageIncidentTicketLink).where(
                    OutageIncidentTicketLink.incident_id == command.incident_id
                )
            )
            is None
        )
        assert (
            db.scalar(
                select(OutageComplaintCorrelationReceipt).where(
                    OutageComplaintCorrelationReceipt.incident_id == command.incident_id
                )
            )
            is None
        )
