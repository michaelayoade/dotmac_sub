"""Pin legacy customer deletion on real Alembic PostgreSQL, including defects.

These are characterization assertions, not approval of the legacy transaction
boundary. Private migrated clones let the real committing entrypoints run.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, delete, inspect
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.customer_account_deletion import require_deleted
from app.models.auth import (
    ApiKey,
    AuthenticationBinding,
    MFAMethod,
    MFAMethodType,
    MFARecoveryCode,
    UserCredential,
)
from app.models.auth import Session as AuthSession
from app.models.billing import Invoice
from app.models.catalog import (
    AccessType,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
)
from app.models.network import (
    CPEDevice,
    DeviceStatus,
    IPAssignment,
    IPv4Address,
    IPv6Address,
    IPVersion,
)
from app.models.notification import (
    CommunicationIntentRecord,
    Notification,
    NotificationChannel,
)
from app.models.party import Party, PartyType
from app.models.subscriber import Reseller, Subscriber
from app.services import web_customer_actions as web
from app.services.audit_adapter import AuditActor
from app.services.customer_account_deletion import (
    CustomerAccountDeletionError,
    CustomerDeletionExpectation,
    CustomerDeletionPolicy,
    DeleteCustomerAccountCommand,
    delete_customer_account,
)
from app.services.owner_commands import CommandContext

ACTOR = AuditActor.user("customer-deletion-test-administrator")


def _delete(
    db: Session,
    customer_id: str,
    *,
    policy: CustomerDeletionPolicy = CustomerDeletionPolicy.JSON,
) -> None:
    context = CommandContext.system(
        actor=f"user:{ACTOR.actor_id}",
        scope="customer:delete",
        reason="Customer deletion integration test",
    )
    with Session(db.get_bind()) as owner_db:
        outcome = delete_customer_account(
            owner_db,
            command=DeleteCustomerAccountCommand(
                context=context,
                actor=ACTOR,
                subscriber_id=UUID(customer_id),
                policy=policy,
            ),
        )
    require_deleted(outcome, business=policy is CustomerDeletionPolicy.WEB_BUSINESS)


def _person(db: Session, customer_id: str) -> None:
    _delete(db, customer_id, policy=CustomerDeletionPolicy.WEB_PERSON)


def _business(db: Session, customer_id: str) -> None:
    _delete(db, customer_id, policy=CustomerDeletionPolicy.WEB_BUSINESS)


@pytest.fixture
def deletion_db(
    cloned_database: Callable[[str], URL], monkeypatch: pytest.MonkeyPatch
) -> Iterator[Session]:
    engine = create_engine(cloned_database("heads"))
    monkeypatch.setattr(
        "app.services.db_session_adapter.SessionLocal", lambda: Session(engine)
    )
    try:
        with Session(engine) as db:
            yield db
    finally:
        engine.dispose()


def _customer(db: Session, *, active: bool = False, business: bool = False) -> UUID:
    row = Subscriber(
        first_name="Deletion",
        last_name="Characterization",
        email=f"{uuid4().hex}@example.test",
        is_active=active,
        metadata_={"subscriber_category": "business" if business else "residential"},
        reseller=Reseller(name=f"Deletion fixture {uuid4().hex}"),
    )
    db.add(row)
    db.flush()
    return row.id


def _fk_name(db: Session, table: str, column: str, parent: str) -> str:
    fk = next(
        fk
        for fk in inspect(db.connection()).get_foreign_keys(table)
        if fk["constrained_columns"] == [column]
    )
    assert fk["referred_table"] == parent
    assert isinstance(fk["name"], str)
    return fk["name"]


def _assert_fk_failure(
    error: CustomerAccountDeletionError, *, table: str, constraint: str
) -> None:
    # Diagnostic identifiers only: do not render SQL, parameters or row details.
    error = error.__cause__
    assert isinstance(error, IntegrityError)
    assert error.orig.sqlstate == "23503"
    assert error.orig.diag.table_name == table
    assert error.orig.diag.constraint_name == constraint


def _subscription(db: Session, customer_id: UUID) -> UUID:
    offer = CatalogOffer(
        name=f"Deletion {uuid4().hex}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
    )
    db.add(offer)
    db.flush()
    row = Subscription(subscriber_id=customer_id, offer_id=offer.id)
    db.add(row)
    db.flush()
    return row.id


def _auth_children(db: Session, customer_id: UUID) -> tuple[UUID, UUID, UUID, UUID]:
    credential = UserCredential(
        subscriber_id=customer_id,
        username=uuid4().hex,
        password_hash="synthetic-test-hash",
    )
    method = MFAMethod(subscriber_id=customer_id, method_type=MFAMethodType.totp)
    session = AuthSession(
        subscriber_id=customer_id,
        token_hash=uuid4().hex,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    key = ApiKey(subscriber_id=customer_id, key_hash=uuid4().hex)
    db.add_all([credential, method, session, key])
    db.flush()
    return credential.id, method.id, session.id, key.id


def _network(db: Session, customer_id: UUID) -> tuple[UUID, UUID, UUID, UUID]:
    v4 = IPv4Address(address="192.0.2.7")
    v6 = IPv6Address(address="2001:db8::7")
    db.add_all([v4, v6])
    db.flush()
    active = CPEDevice(subscriber_id=customer_id, status=DeviceStatus.active)
    retired = CPEDevice(subscriber_id=customer_id, status=DeviceStatus.retired)
    a4 = IPAssignment(
        subscriber_id=customer_id,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=v4.id,
        is_active=True,
    )
    a6 = IPAssignment(
        subscriber_id=customer_id,
        ip_version=IPVersion.ipv6,
        ipv6_address_id=v6.id,
        is_active=True,
    )
    db.add_all([active, retired, a4, a6])
    db.flush()
    return active.id, retired.id, a4.id, a6.id


def test_migrated_cpe_subscriber_is_nullable_set_null(deletion_db: Session) -> None:
    catalog = inspect(deletion_db.connection())
    column = next(
        col
        for col in catalog.get_columns("cpe_devices")
        if col["name"] == "subscriber_id"
    )
    fk = next(
        fk
        for fk in catalog.get_foreign_keys("cpe_devices")
        if fk["constrained_columns"] == ["subscriber_id"]
    )
    assert column["nullable"] is True
    assert fk["referred_table"] == "subscribers"
    assert fk["options"]["ondelete"] == "SET NULL"


def test_actual_parent_delete_retires_and_detaches_network(
    deletion_db: Session,
) -> None:
    db = deletion_db
    customer_id = _customer(db)
    active_id, retired_id, a4_id, a6_id = _network(db, customer_id)
    db.commit()

    _delete(db, str(customer_id))

    db.expire_all()
    assert db.get(Subscriber, customer_id) is None
    for device_id in (active_id, retired_id):
        device = db.get(CPEDevice, device_id)
        assert device is not None
        assert device.status == DeviceStatus.retired
        assert device.subscriber_id is None
    for assignment_id in (a4_id, a6_id):
        assignment = db.get(IPAssignment, assignment_id)
        assert assignment is not None
        assert assignment.is_active is False
        assert assignment.subscriber_id is None


def test_database_fk_retains_cpe_without_legacy_release_helper(
    deletion_db: Session,
) -> None:
    db = deletion_db
    customer_id = _customer(db)
    device = CPEDevice(subscriber_id=customer_id, status=DeviceStatus.active)
    db.add(device)
    db.commit()
    device_id = device.id
    db.execute(delete(Subscriber).where(Subscriber.id == customer_id))
    db.commit()
    db.expire_all()
    retained = db.get(CPEDevice, device_id)
    assert retained is not None
    assert retained.subscriber_id is None
    assert retained.status == DeviceStatus.active


def test_parent_financial_blocker_rolls_back_network_changes(
    deletion_db: Session,
) -> None:
    db = deletion_db
    customer_id = _customer(db)
    active_id, retired_id, a4_id, a6_id = _network(db, customer_id)
    invoice = Invoice(account_id=customer_id)
    db.add(invoice)
    constraint = _fk_name(db, "invoices", "account_id", "subscribers")
    db.commit()

    with pytest.raises(CustomerAccountDeletionError) as caught:
        _delete(db, str(customer_id))
    db.rollback()
    _assert_fk_failure(caught.value, table="invoices", constraint=constraint)

    assert db.get(Subscriber, customer_id) is not None
    assert db.get(CPEDevice, active_id).status == DeviceStatus.active
    assert db.get(CPEDevice, retired_id).status == DeviceStatus.retired
    for assignment_id in (a4_id, a6_id):
        assignment = db.get(IPAssignment, assignment_id)
        assert assignment.is_active is True
        assert assignment.subscriber_id == customer_id


@pytest.mark.parametrize(
    "status", [DeviceStatus.inactive, DeviceStatus.maintenance, DeviceStatus.draining]
)
def test_nonactive_cpe_status_unchanged_on_parent_delete(
    deletion_db: Session, status: DeviceStatus
) -> None:
    db = deletion_db
    customer_id = _customer(db)
    device = CPEDevice(subscriber_id=customer_id, status=status)
    db.add(device)
    db.commit()
    device_id = device.id
    _delete(db, str(customer_id))
    db.expire_all()
    retained = db.get(CPEDevice, device_id)
    assert retained is not None
    assert retained.status == status
    assert retained.subscriber_id is None


@pytest.mark.parametrize("kind", ["person", "business"])
def test_web_rejects_active_customer(deletion_db: Session, kind: str) -> None:
    db = deletion_db
    customer_id = _customer(db, active=True, business=kind == "business")
    child_ids = _auth_children(db, customer_id)
    db.commit()
    action = _person if kind == "person" else _business
    with pytest.raises(HTTPException) as caught:
        action(db, str(customer_id))
    assert caught.value.status_code == 409
    assert caught.value.detail == "Deactivate customer before deleting."
    assert db.get(Subscriber, customer_id) is not None
    assert db.get(ApiKey, child_ids[3]) is not None


def test_json_accepts_active_customer_without_auth_children(
    deletion_db: Session,
) -> None:
    customer_id = _customer(deletion_db, active=True)
    deletion_db.commit()
    _delete(deletion_db, str(customer_id))
    assert deletion_db.get(Subscriber, customer_id) is None


def test_json_does_not_implicitly_delete_auth_children(deletion_db: Session) -> None:
    db = deletion_db
    customer_id = _customer(db)
    credential = UserCredential(
        subscriber_id=customer_id,
        username=uuid4().hex,
        password_hash="synthetic-test-hash",
    )
    db.add(credential)
    db.flush()
    credential_id = credential.id
    constraint = _fk_name(db, "user_credentials", "subscriber_id", "subscribers")
    db.commit()
    with pytest.raises(CustomerAccountDeletionError) as caught:
        _delete(db, str(customer_id))
    db.rollback()
    _assert_fk_failure(caught.value, table="user_credentials", constraint=constraint)
    assert db.get(Subscriber, customer_id) is not None
    assert db.get(UserCredential, credential_id) is not None


def test_web_success_removes_existing_auth_children(deletion_db: Session) -> None:
    db = deletion_db
    customer_id = _customer(db)
    ids = _auth_children(db, customer_id)
    db.commit()
    _person(db, str(customer_id))
    assert db.get(Subscriber, customer_id) is None
    for model, child_id in zip(
        (UserCredential, MFAMethod, AuthSession, ApiKey), ids, strict=True
    ):
        assert db.get(model, child_id) is None


def test_web_parent_failure_atomically_preserves_auth(
    deletion_db: Session,
) -> None:
    db = deletion_db
    customer_id = _customer(db)
    ids = _auth_children(db, customer_id)
    db.add(Invoice(account_id=customer_id))
    constraint = _fk_name(db, "invoices", "account_id", "subscribers")
    db.commit()
    with pytest.raises(CustomerAccountDeletionError) as caught:
        _person(db, str(customer_id))
    db.rollback()
    _assert_fk_failure(caught.value, table="invoices", constraint=constraint)
    assert db.get(Subscriber, customer_id) is not None
    for model, child_id in zip(
        (UserCredential, MFAMethod, AuthSession, ApiKey), ids, strict=True
    ):
        assert db.get(model, child_id) is not None


def test_mfa_recovery_fk_blocks_web_cleanup_before_commit(deletion_db: Session) -> None:
    db = deletion_db
    customer_id = _customer(db)
    ids = _auth_children(db, customer_id)
    recovery = MFARecoveryCode(mfa_method_id=ids[1], code_hash=uuid4().hex)
    db.add(recovery)
    constraint = _fk_name(db, "mfa_recovery_codes", "mfa_method_id", "mfa_methods")
    db.commit()
    recovery_id = recovery.id
    with pytest.raises(CustomerAccountDeletionError) as caught:
        _person(db, str(customer_id))
    db.rollback()
    _assert_fk_failure(caught.value, table="mfa_recovery_codes", constraint=constraint)
    assert db.get(Subscriber, customer_id) is not None
    assert db.get(MFARecoveryCode, recovery_id) is not None
    for model, child_id in zip(
        (UserCredential, MFAMethod, AuthSession, ApiKey), ids, strict=True
    ):
        assert db.get(model, child_id) is not None


def test_business_subscription_precheck_preserves_auth(deletion_db: Session) -> None:
    db = deletion_db
    customer_id = _customer(db, business=True)
    key_id = _auth_children(db, customer_id)[3]
    subscription_id = _subscription(db, customer_id)
    db.commit()
    with pytest.raises(HTTPException) as caught:
        _business(db, str(customer_id))
    assert caught.value.status_code == 409
    assert (
        caught.value.detail == "Delete subscriptions before deleting business customer."
    )
    assert db.get(ApiKey, key_id) is not None
    assert db.get(Subscription, subscription_id) is not None


def test_person_subscription_blocker_atomically_preserves_auth(
    deletion_db: Session,
) -> None:
    db = deletion_db
    customer_id = _customer(db)
    key_id = _auth_children(db, customer_id)[3]
    subscription_id = _subscription(db, customer_id)
    db.commit()
    with pytest.raises(CustomerAccountDeletionError) as caught:
        _person(db, str(customer_id))
    db.rollback()
    failure = caught.value.__cause__
    assert isinstance(failure, IntegrityError)
    assert failure.orig.sqlstate == "23502"
    assert failure.orig.diag.table_name == "subscriptions"
    assert failure.orig.diag.column_name == "subscriber_id"
    assert db.get(Subscriber, customer_id) is not None
    assert db.get(Subscription, subscription_id) is not None
    assert db.get(ApiKey, key_id) is not None


def test_bulk_preserves_earlier_success_and_skips_ineligible_items(
    deletion_db: Session,
) -> None:
    db = deletion_db
    first_id = _customer(db)
    active_id = _customer(db, active=True)
    subscribed_id = _customer(db, business=True)
    _subscription(db, subscribed_id)
    missing_id = uuid4()
    last_id = _customer(db, business=True)
    db.commit()
    result = web.bulk_delete_customers(
        db,
        [
            {"id": str(first_id), "type": "person"},
            {"id": str(active_id), "type": "person"},
            {"id": str(subscribed_id), "type": "business"},
            {"id": str(missing_id), "type": "subscriber"},
            {"id": str(last_id), "type": "business"},
        ],
        actor=ACTOR,
        expectations=tuple(
            CustomerDeletionExpectation(item, active, subscribed)
            for item, active, subscribed in [
                (first_id, False, False),
                (active_id, True, False),
                (subscribed_id, False, True),
                (missing_id, False, False),
                (last_id, False, False),
            ]
        ),
    )
    assert result == {
        "success": True,
        "deleted_count": 2,
        "skipped": [
            {
                "id": str(active_id),
                "type": "person",
                "reason": "Customer is still active",
            },
            {
                "id": str(subscribed_id),
                "type": "business",
                "reason": "Has associated subscriptions",
            },
            {
                "id": str(missing_id),
                "type": "subscriber",
                "reason": "Subscriber not found",
            },
        ],
    }
    assert db.get(Subscriber, first_id) is None
    assert db.get(Subscriber, last_id) is None
    assert db.get(Subscriber, active_id) is not None
    assert db.get(Subscriber, subscribed_id) is not None


def test_party_binding_preserved_intent_cascades_notification_detaches(
    deletion_db: Session,
) -> None:
    db = deletion_db
    party = Party(party_type=PartyType.person.value, display_name="Deletion Party")
    binding = AuthenticationBinding(
        binding_key=uuid4().hex,
        mechanism_code="local_password",
        name="Deletion binding",
    )
    db.add_all([party, binding])
    db.flush()
    customer_id = _customer(db)
    customer = db.get(Subscriber, customer_id)
    customer.party_id = party.id
    customer.party_bound_at = datetime.now(UTC)
    customer.party_binding_source = "integration_test"
    customer.party_binding_reason = "characterize account removal"
    credential = UserCredential(
        subscriber_id=customer_id,
        username=uuid4().hex,
        password_hash="synthetic-test-hash",
    )
    intent = CommunicationIntentRecord(
        subscriber_id=customer_id,
        event_type="test",
        category="test",
        communication_class="transactional",
    )
    db.add_all([credential, intent])
    db.flush()
    notification = Notification(
        subscriber_id=customer_id,
        communication_intent_id=intent.id,
        channel=NotificationChannel.email,
        recipient="deletion@example.test",
    )
    db.add(notification)
    db.commit()
    party_id, binding_id, intent_id, notification_id = (
        party.id,
        binding.id,
        intent.id,
        notification.id,
    )
    _person(db, str(customer_id))
    db.expire_all()
    assert db.get(Party, party_id) is not None
    assert db.get(AuthenticationBinding, binding_id) is not None
    assert db.get(CommunicationIntentRecord, intent_id) is None
    retained = db.get(Notification, notification_id)
    assert retained is not None
    assert retained.subscriber_id is None
    assert retained.communication_intent_id is None
