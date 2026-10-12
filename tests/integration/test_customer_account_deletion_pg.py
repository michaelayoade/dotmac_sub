# ruff: noqa: F811
"""Atomic customer deletion on private real-migration PostgreSQL clones."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.auth import ApiKey, MFAMethod, UserCredential
from app.models.auth import Session as AuthSession
from app.models.billing import Invoice
from app.models.subscriber import Subscriber, UserType
from app.services import customer_account_deletion as owner
from app.services import web_customer_actions as web
from app.services.owner_commands import CommandContext
from tests.integration.test_customer_hard_delete_characterization_pg import (
    ACTOR,
    _auth_children,
    _customer,
    _network,
    _person,
    deletion_db,  # noqa: F401
)


def _command(customer_id: UUID) -> owner.DeleteCustomerAccountCommand:
    return owner.DeleteCustomerAccountCommand(
        context=CommandContext.system(
            actor=f"user:{ACTOR.actor_id}",
            scope=owner.SCOPE,
            reason="Atomic deletion integration",
        ),
        actor=ACTOR,
        subscriber_id=customer_id,
        policy=owner.CustomerDeletionPolicy.WEB_PERSON,
    )


def test_audit_failure_rolls_back_parent_auth_network(deletion_db, monkeypatch):
    db = deletion_db
    customer_id = _customer(db)
    child_ids = _auth_children(db, customer_id)
    cpe_id, _, ip_id, _ = _network(db, customer_id)
    db.commit()

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic audit unavailable")

    monkeypatch.setattr(owner, "stage_audit_event", fail)
    with pytest.raises(owner.CustomerAccountDeletionError) as caught:
        _person(db, str(customer_id))
    assert caught.value.code.endswith(".unavailable")
    assert db.get(Subscriber, customer_id) is not None
    for model, child_id in zip(
        (UserCredential, MFAMethod, AuthSession, ApiKey), child_ids, strict=True
    ):
        assert db.get(model, child_id) is not None
    from app.models.network import CPEDevice, DeviceStatus, IPAssignment

    assert db.get(CPEDevice, cpe_id).status is DeviceStatus.active
    assert db.get(IPAssignment, ip_id).is_active
    assert (
        db.scalar(
            select(AuditEvent.id).where(
                AuditEvent.action == "customer.account.deleted",
                AuditEvent.entity_id == str(customer_id),
            )
        )
        is None
    )


@pytest.mark.parametrize(
    "model,index", [(ApiKey, 3), (UserCredential, 0), (MFAMethod, 1), (AuthSession, 2)]
)
def test_child_first_concurrent_writer_refuses_without_parent_wait_cycle(
    deletion_db, model, index
):
    db = deletion_db
    customer_id = _customer(db)
    child_ids = _auth_children(db, customer_id)
    db.commit()
    # A physical connection holds the same row first as key PATCH, credential
    # login/reset, factor counters and session refresh writers do.
    with Session(db.get_bind()) as writer:
        writer.execute(
            select(model.id).where(model.id == child_ids[index]).with_for_update()
        )
        with Session(db.get_bind()) as command_db:
            with pytest.raises(owner.CustomerAccountDeletionError) as caught:
                owner.delete_customer_account(command_db, command=_command(customer_id))
            assert caught.value.code.endswith(".busy")
            assert not command_db.in_transaction()
        writer.rollback()
    assert db.get(Subscriber, customer_id) is not None
    assert db.get(model, child_ids[index]) is not None


def test_canonical_audit_commits_with_actual_deletion(deletion_db):
    db = deletion_db
    customer_id = _customer(db)
    db.commit()
    _person(db, str(customer_id))
    audit = db.scalars(
        select(AuditEvent).where(
            AuditEvent.action == "customer.account.deleted",
            AuditEvent.entity_id == str(customer_id),
        )
    ).one()
    assert audit.actor_id == ACTOR.actor_id
    assert db.get(Subscriber, customer_id) is None


def test_bulk_failed_middle_preserves_earlier_success_and_continues(deletion_db):
    db = deletion_db
    first, middle, last = (_customer(db) for _ in range(3))
    db.add(Invoice(account_id=middle))
    children = _auth_children(db, middle)
    db.commit()
    result = web.bulk_delete_customers(
        db,
        [
            {"id": str(first), "type": "person"},
            {"id": str(middle), "type": "business"},
            {"id": str(first), "type": "subscriber"},
            {"id": str(uuid4()), "type": "unknown"},
            {"id": str(last), "type": "person"},
        ],
        actor=ACTOR,
        expectations=tuple(
            owner.CustomerDeletionExpectation(item, False, False)
            for item in (first, middle, last)
        ),
    )
    assert result["deleted_count"] == 2
    assert len(result["skipped"]) == 3
    assert result["skipped"][1]["reason"] == "Subscriber not found"
    assert result["skipped"][2]["reason"] == "Unknown customer type"
    assert db.get(Subscriber, first) is None
    assert db.get(Subscriber, last) is None
    assert db.get(Subscriber, middle) is not None
    assert db.get(ApiKey, children[3]) is not None


def test_adapter_preserves_flushed_caller_writes(deletion_db):
    db = deletion_db
    customer_id = _customer(db)
    db.commit()
    caller_id = _customer(db)
    db.flush()
    web.delete_person_customer(db, str(customer_id), actor=ACTOR)
    assert db.in_transaction()
    assert db.get(Subscriber, caller_id) is not None
    db.rollback()
    assert db.get(Subscriber, caller_id) is None
    assert db.get(Subscriber, customer_id) is None


def test_key_transferred_between_capture_and_lock_is_preserved(deletion_db):
    db = deletion_db
    customer_id, destination_id = _customer(db), _customer(db)
    key = ApiKey(subscriber_id=customer_id, key_hash=uuid4().hex)
    db.add(key)
    db.commit()
    key_id = key.id
    with Session(db.get_bind()) as command_db:
        moved = False

        def transfer(connection, clause, multiparams, params, execution_options):
            nonlocal moved
            if (
                not moved
                and getattr(clause, "_for_update_arg", None) is not None
                and any(
                    getattr(table, "name", None) == "api_keys"
                    for table in clause.get_final_froms()
                )
            ):
                moved = True
                with Session(db.get_bind()) as writer:
                    row = writer.get(ApiKey, key_id)
                    row.subscriber_id = destination_id
                    writer.commit()

        event.listen(command_db.get_bind(), "before_execute", transfer)
        try:
            result = owner.delete_customer_account(
                command_db, command=_command(customer_id)
            )
        finally:
            event.remove(command_db.get_bind(), "before_execute", transfer)
    assert moved
    assert result.status is owner.CustomerDeletionStatus.DELETED
    db.expire_all()
    assert db.get(ApiKey, key_id).subscriber_id == destination_id


def test_key_created_after_capture_before_parent_refuses_stale_capture(deletion_db):
    db = deletion_db
    customer_id = _customer(db)
    db.commit()
    with Session(db.get_bind()) as command_db:
        inserted = False

        def create(connection, clause, multiparams, params, execution_options):
            nonlocal inserted
            if (
                not inserted
                and getattr(clause, "_for_update_arg", None) is not None
                and any(
                    getattr(table, "name", None) == "subscribers"
                    for table in clause.get_final_froms()
                )
            ):
                inserted = True
                with Session(db.get_bind()) as writer:
                    writer.add(ApiKey(subscriber_id=customer_id, key_hash=uuid4().hex))
                    writer.commit()

        event.listen(command_db.get_bind(), "before_execute", create)
        try:
            with pytest.raises(owner.CustomerAccountDeletionError) as caught:
                owner.delete_customer_account(command_db, command=_command(customer_id))
            assert caught.value.code.endswith(".busy")
            assert not command_db.in_transaction()
        finally:
            event.remove(command_db.get_bind(), "before_execute", create)
    assert inserted
    assert db.get(Subscriber, customer_id) is not None


def test_referral_first_enrollment_lock_is_a_preserved_nonwaiting_blocker(deletion_db):
    from app.models.referral_native import Referral

    db = deletion_db
    customer_id, referrer_id = _customer(db), _customer(db)
    referral = Referral(
        referrer_subscriber_id=referrer_id, referred_subscriber_id=customer_id
    )
    db.add(referral)
    db.commit()
    referral_id = referral.id
    with Session(db.get_bind()) as enrollment:
        enrollment.execute(
            select(Referral.id).where(Referral.id == referral_id).with_for_update()
        )
        with Session(db.get_bind()) as command_db:
            with pytest.raises(owner.CustomerAccountDeletionError) as caught:
                owner.delete_customer_account(command_db, command=_command(customer_id))
            assert caught.value.code.endswith(".blocked")
            assert not command_db.in_transaction()
        # Deletion released its parent lock while this enrollment still holds
        # Referral: the concrete inverse order cannot wait on parent DELETE.
        enrollment.execute(
            select(Subscriber.id)
            .where(Subscriber.id == customer_id)
            .with_for_update(nowait=True)
        )
        enrollment.rollback()


def test_existing_orm_children_cascade_ont_detaches_global_party_preserved(deletion_db):
    from app.models.network import OntAssignment, OntUnit
    from app.models.party import (
        Party,
        PartyContactPoint,
        PartyRelationship,
        PartyType,
        SubscriberContactPointProjection,
        SubscriberContactRelationshipProjection,
    )
    from app.models.subscriber import (
        Address,
        ChannelType,
        SubscriberChannel,
        SubscriberContact,
        SubscriberCustomField,
        SubscriberNINVerification,
    )

    db = deletion_db
    customer_id = _customer(db)
    person, organization = (
        Party(party_type=PartyType.person.value, display_name="Fixture contact"),
        Party(
            party_type=PartyType.organization.value, display_name="Fixture organization"
        ),
    )
    db.add_all([person, organization])
    db.flush()
    point = PartyContactPoint(
        party_id=person.id,
        channel_type="email",
        normalized_value="fixture@example.test",
    )
    relationship = PartyRelationship(
        subject_party_id=person.id,
        object_party_id=organization.id,
        relationship_type="contact_for",
    )
    contact = SubscriberContact(subscriber_id=customer_id, full_name="Fixture")
    ont = OntUnit(serial_number=uuid4().hex)
    db.add_all([point, relationship, contact, ont])
    db.flush()
    projection = SubscriberContactPointProjection(
        subscriber_contact_id=contact.id,
        party_contact_point_id=point.id,
        source_field="email",
        binding_source="integration_test",
        binding_reason="fixture",
    )
    relationship_projection = SubscriberContactRelationshipProjection(
        subscriber_contact_id=contact.id,
        party_relationship_id=relationship.id,
        binding_source="integration_test",
        binding_reason="fixture",
    )
    assignment = OntAssignment(subscriber_id=customer_id, ont_unit_id=ont.id)
    children = [
        Address(subscriber_id=customer_id, address_line1="Fixture road"),
        SubscriberCustomField(subscriber_id=customer_id, key="fixture"),
        SubscriberChannel(
            subscriber_id=customer_id,
            channel_type=ChannelType.email,
            address="fixture@example.test",
        ),
        SubscriberNINVerification(subscriber_id=customer_id, nin="12345678901"),
        contact,
        projection,
        relationship_projection,
    ]
    db.add_all([*children, assignment])
    db.commit()
    children_ids = [(type(child), child.id) for child in children]
    retained_ids = [
        (type(row), row.id) for row in [person, organization, point, relationship, ont]
    ]
    assignment_id = assignment.id
    _person(db, str(customer_id))
    db.expire_all()
    for model, row_id in children_ids:
        assert db.get(model, row_id) is None
    for model, row_id in retained_ids:
        assert db.get(model, row_id) is not None
    assert db.get(OntAssignment, assignment_id).subscriber_id is None


@pytest.mark.parametrize(
    "previous_active,previous_subscribed", [(True, False), (False, True)]
)
def test_verified_bulk_expectation_revalidated_after_parent_lock(
    deletion_db, previous_active, previous_subscribed
):
    # The preview described an ineligible account; after confirmation another
    # connection removed the blocker. The old preview must never authorize it.
    db = deletion_db
    customer_id = _customer(db)
    child_ids = _auth_children(db, customer_id)
    db.commit()
    command = owner.DeleteCustomerAccountCommand(
        context=CommandContext.system(
            actor=f"user:{ACTOR.actor_id}",
            scope=owner.SCOPE,
            reason="Confirmed preview",
        ),
        actor=ACTOR,
        subscriber_id=customer_id,
        policy=owner.CustomerDeletionPolicy.WEB_BULK,
        expected_eligibility=owner.CustomerDeletionExpectation(
            customer_id, previous_active, previous_subscribed
        ),
    )
    with Session(db.get_bind()) as command_db:
        with pytest.raises(owner.CustomerAccountDeletionError) as caught:
            owner.delete_customer_account(command_db, command=command)
        assert caught.value.code.endswith(".stale_impact")
        assert not command_db.in_transaction()
    assert db.get(Subscriber, customer_id) is not None
    assert db.get(ApiKey, child_ids[3]) is not None


@pytest.mark.parametrize("blocker", ["active", "subscription"])
def test_payload_confirmation_then_concurrent_eligibility_change_never_expands_delete(
    deletion_db, monkeypatch, blocker
):
    from sqlalchemy import delete

    from app.models.catalog import Subscription
    from tests.integration.test_customer_hard_delete_characterization_pg import (
        _subscription,
    )

    db = deletion_db
    customer_id = _customer(db, active=blocker == "active")
    # The shared hard-delete fixture defaults to a system user; the actual
    # customer-list selector admits only explicitly classified customers.
    customer = db.get(Subscriber, customer_id)
    assert customer is not None
    customer.user_type = UserType.customer
    if blocker == "subscription":
        _subscription(db, customer_id)
    db.commit()
    preview = web.bulk_delete_customers_from_payload(
        db,
        {"customer_ids": [{"id": str(customer_id)}], "preview_only": True},
        actor=ACTOR,
    )
    assert preview["matched_count"] == 1
    assert preview["impact"]["eligible"] == 0
    assert (
        preview["impact"]["active" if blocker == "active" else "with_subscriptions"]
        == 1
    )
    original = web._require_bulk_execution_confirmation

    def confirm_then_change(*args, **kwargs):
        original(*args, **kwargs)
        with Session(db.get_bind()) as writer:
            if blocker == "active":
                writer.get(Subscriber, customer_id).is_active = False
            else:
                writer.execute(
                    delete(Subscription).where(
                        Subscription.subscriber_id == customer_id
                    )
                )
            writer.commit()

    monkeypatch.setattr(
        web, "_require_bulk_execution_confirmation", confirm_then_change
    )
    result = web.bulk_delete_customers_from_payload(
        db,
        {
            "selection": {
                "mode": "selected",
                "ids": [{"id": str(customer_id)}],
                "expected_count": preview["matched_count"],
                "expected_scope_token": preview["scope_token"],
            },
            "confirmed": True,
        },
        actor=ACTOR,
    )
    assert result["deleted_count"] == 0
    assert (
        result["skipped"][0]["reason"]
        == "Customer deletion impact changed. Preview again."
    )
    db.expire_all()
    assert db.get(Subscriber, customer_id) is not None
