"""Migrated PostgreSQL proofs for native Test Connection creation counts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.catalog import (
    AccessType,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.event_store import EventStore
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.models.test_connection import TestConnectionGrant as Grant
from app.services import test_connection as owner
from app.services.events.types import EventType
from app.services.owner_commands import CommandContext
from app.services.subscriber import _default_reseller_id

NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolate_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.events.dispatcher.run_after_commit", lambda *_: None
    )
    monkeypatch.setattr(
        owner,
        "configuration",
        lambda db: owner.TestConnectionConfiguration(2, 24, True),
    )
    # Network admission has separate real-RADIUS tests in the merged feature.
    monkeypatch.setattr(owner, "_validate_network_identity", lambda *_: None)


def _seed(db: Session) -> tuple[UUID, tuple[UUID, UUID], UUID]:
    account = Subscriber(
        first_name="Finance",
        last_name="Concurrency",
        email=f"account-{uuid4()}@example.com",
        reseller_id=_default_reseller_id(db),
    )
    offer = CatalogOffer(
        name=f"Finance Test {uuid4()}",
        code=f"test-{uuid4()}",
        access_type=AccessType.fiber,
        service_type=ServiceType.residential,
        price_basis=PriceBasis.flat,
    )
    actor = SystemUser(
        first_name="Test",
        last_name="Staff",
        email=f"staff-{uuid4()}@example.com",
        is_active=True,
    )
    db.add_all([account, offer, actor])
    db.flush()
    subs = tuple(
        Subscription(
            subscriber_id=account.id,
            offer_id=offer.id,
            status=SubscriptionStatus.suspended,
            login=f"login-{uuid4()}",
        )
        for _ in range(2)
    )
    db.add_all(subs)
    db.flush()
    result = account.id, (subs[0].id, subs[1].id), actor.id
    for _ in range(4):
        db.add(
            Grant(
                subscriber_id=account.id,
                subscription_id=subs[0].id,
                actor_id=actor.id,
                actor_label="History",
                command_id=uuid4(),
                activated_at=datetime.now(UTC) - timedelta(days=1),
                expires_at=datetime.now(UTC) - timedelta(hours=23),
                ended_at=datetime.now(UTC) - timedelta(hours=23),
                duration_seconds=3600,
                delivery_state="applied",
            )
        )
    db.commit()
    return result


def _command(
    account_id: UUID, sub_id: UUID, actor_id: UUID, key: UUID
) -> owner.ActivateTestConnectionCommand:
    return owner.ActivateTestConnectionCommand(
        subscriber_id=account_id,
        subscription_id=sub_id,
        actor_id=actor_id,
        context=CommandContext(
            command_id=key,
            correlation_id=key,
            actor=str(actor_id),
            scope=owner.PERMISSION,
            reason="Verify counting",
            idempotency_key=str(key),
        ),
    )


@pytest.mark.parametrize("same_key", (False, True))
def test_same_customer_parallel_native_creations_count_once(
    engine, same_key: bool
) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        account_id, sub_ids, actor_id = _seed(db)
    barrier = Barrier(2)
    shared_key = uuid4()

    def worker(index: int) -> tuple[UUID, bool, int]:
        with factory() as db:
            barrier.wait(timeout=15)
            outcome = owner.activate_test_connection(
                db,
                command=_command(
                    account_id,
                    sub_ids[0 if same_key else index],
                    actor_id,
                    shared_key if same_key else uuid4(),
                ),
            )
            event = db.scalar(
                select(EventStore).where(
                    EventStore.event_type == EventType.test_connection_created.value,
                    EventStore.payload["grant_id"].astext == str(outcome.grant_id),
                )
            )
            assert event is not None
            return outcome.grant_id, outcome.replayed, event.payload["count_7d"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(worker, range(2)))
    assert sorted(item[2] for item in outcomes) == ([5, 5] if same_key else [5, 6])
    assert len({item[0] for item in outcomes}) == (1 if same_key else 2)
    if same_key:
        assert sorted(item[1] for item in outcomes) == [False, True]


def test_migrated_receipt_uniqueness_and_native_grant_schema(engine) -> None:
    inspector = inspect(engine)
    assert "test_connection_grants" in inspector.get_table_names()
    assert "uq_test_connection_review_step" in {
        item["name"]
        for item in inspector.get_unique_constraints("test_connection_finance_reviews")
    }
    assert "ck_test_connection_review_step" in {
        item["name"]
        for item in inspector.get_check_constraints("test_connection_finance_reviews")
    }


def test_native_creation_event_failure_rolls_back_grant_and_outbox(
    engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        account_id, sub_ids, actor_id = _seed(db)
    original = owner._stage_finance_creation_event

    def fail_after_staging(
        db: Session, *, grant: Grant, context: CommandContext
    ) -> None:
        original(db, grant=grant, context=context)
        raise RuntimeError("injected creation-event failure")

    monkeypatch.setattr(owner, "_stage_finance_creation_event", fail_after_staging)
    with factory() as db:
        with pytest.raises(RuntimeError, match="injected"):
            owner.activate_test_connection(
                db, command=_command(account_id, sub_ids[1], actor_id, uuid4())
            )
        assert db.query(Grant).filter_by(subscription_id=sub_ids[1]).count() == 0
        assert (
            db.query(EventStore)
            .filter_by(
                event_type=EventType.test_connection_created.value,
                account_id=account_id,
            )
            .count()
            == 0
        )
