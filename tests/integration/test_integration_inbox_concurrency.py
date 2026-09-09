"""PostgreSQL concurrency contract for verified integration receipts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

from sqlalchemy.orm import sessionmaker

from app.models.integration_platform import IntegrationInbox
from app.services.integrations import inbox
from app.services.integrations.whatsapp_capability import WHATSAPP_RECEIVE_CAPABILITY
from tests.test_integration_whatsapp_capability import install_whatsapp


def test_concurrent_provider_replay_creates_and_claims_one_receipt(engine) -> None:
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with session_factory() as setup:
        _installation, bindings = install_whatsapp(setup)
        binding_id = bindings[WHATSAPP_RECEIVE_CAPABILITY].id
        setup.commit()

    ready = Barrier(2)

    def receive() -> tuple[str, bool]:
        with session_factory() as session:
            ready.wait(timeout=5)
            receipt, should_process = inbox.receive_and_claim_verified(
                session,
                capability_binding_id=binding_id,
                provider_event_id="meta:concurrent-replay",
                event_type="whatsapp.meta.webhook",
                payload={"entry": [{"id": "same-event"}]},
            )
            return str(receipt.id), should_process

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: receive(), range(2)))

    assert len({receipt_id for receipt_id, _claimed in outcomes}) == 1
    assert sorted(claimed for _receipt_id, claimed in outcomes) == [False, True]
    with session_factory() as check:
        assert (
            check.query(IntegrationInbox)
            .filter(IntegrationInbox.capability_binding_id == binding_id)
            .filter(IntegrationInbox.provider_event_id == "meta:concurrent-replay")
            .count()
            == 1
        )


def test_concurrent_reclaim_of_one_expired_lease_settles_on_one_attempt(
    engine,
) -> None:
    """Two redeliveries racing to reclaim the SAME expired-lease claim must not
    both succeed. `claim_for_processing`'s reclaim mutation is only made safe
    by the capability-binding lock `receive_verified` takes before reading the
    receipt (SQLite's StaticPool gives every session the same connection, so
    it cannot exercise real blocking — this needs Postgres)."""

    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with session_factory() as setup:
        _installation, bindings = install_whatsapp(setup)
        binding_id = bindings[WHATSAPP_RECEIVE_CAPABILITY].id
        original_claim_time = datetime.now(UTC) - timedelta(hours=1)
        receipt, should_process = inbox.receive_and_claim_verified(
            setup,
            capability_binding_id=binding_id,
            provider_event_id="meta:concurrent-reclaim",
            event_type="whatsapp.meta.webhook",
            payload={"entry": [{"id": "stuck-event"}]},
            now=original_claim_time,
        )
        setup.commit()
        assert should_process is True
        assert receipt.attempt_count == 1

    ready = Barrier(2)
    reclaim_time = datetime.now(UTC)

    def reclaim() -> bool:
        with session_factory() as session:
            ready.wait(timeout=5)
            _reclaimed, should_process_again = inbox.receive_and_claim_verified(
                session,
                capability_binding_id=binding_id,
                provider_event_id="meta:concurrent-reclaim",
                event_type="whatsapp.meta.webhook",
                payload={"entry": [{"id": "stuck-event"}]},
                now=reclaim_time,
            )
            return should_process_again

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: reclaim(), range(2)))

    # Exactly one of the two racing redeliveries reclaimed the row; the other
    # observed a claim that was, by the time it acquired the binding lock,
    # already live again under the new attempt.
    assert sorted(outcomes) == [False, True]
    with session_factory() as check:
        final = (
            check.query(IntegrationInbox)
            .filter(IntegrationInbox.capability_binding_id == binding_id)
            .filter(IntegrationInbox.provider_event_id == "meta:concurrent-reclaim")
            .one()
        )
        assert final.attempt_count == 2
        assert final.state == "processing"
