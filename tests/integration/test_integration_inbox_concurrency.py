"""PostgreSQL concurrency contract for verified integration receipts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
