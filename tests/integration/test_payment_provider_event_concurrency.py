"""PostgreSQL first-insert serialization for provider observations."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy.orm import sessionmaker

from app.models.audit import AuditEvent
from app.models.billing import (
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderType,
)
from app.models.event_store import EventStore
from app.services.owner_commands import CommandContext
from app.services.payment_provider_events import (
    ADMINISTRATIVE_INGEST_SCOPE,
    PaymentProviderEventCommand,
    PaymentProviderEvents,
)


def test_concurrent_exact_provider_observation_is_recorded_once(engine):
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    with session_factory() as setup:
        provider = PaymentProvider(
            name=f"Provider Concurrency {suffix}",
            provider_type=PaymentProviderType.custom,
            is_active=True,
        )
        setup.add(provider)
        setup.commit()
        provider_id = provider.id

    identity = f"provider-concurrency-{suffix}"
    command = PaymentProviderEventCommand(
        provider_id=provider_id,
        event_type="provider.informational_notice",
        external_id=identity,
        payload={"identity": identity},
    )
    barrier = Barrier(2)

    def ingest() -> tuple[uuid.UUID, bool]:
        with session_factory() as worker:
            barrier.wait(timeout=10)
            result = PaymentProviderEvents.ingest(
                worker,
                command,
                context=CommandContext.system(
                    actor="pytest:provider-event-concurrency",
                    scope=ADMINISTRATIVE_INGEST_SCOPE,
                    reason="Verify PostgreSQL provider-event first-insert locking",
                    idempotency_key=identity,
                ),
            )
            return result.id, result.replayed

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: ingest(), range(2)))

    assert len({result[0] for result in results}) == 1
    assert sorted(result[1] for result in results) == [False, True]

    event_id = results[0][0]
    with session_factory() as check:
        assert check.query(PaymentProviderEvent).filter_by(id=event_id).count() == 1
        assert (
            check.query(AuditEvent)
            .filter_by(entity_type="payment_provider_event", entity_id=str(event_id))
            .count()
            == 1
        )
        stored_events = (
            check.query(EventStore)
            .filter_by(event_type="payment_provider_event.processed")
            .all()
        )
        assert (
            sum(
                item.payload.get("aggregate_id") == str(event_id)
                for item in stored_events
            )
            == 1
        )
