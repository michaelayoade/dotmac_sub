"""PostgreSQL proof that concurrent customer retries issue one Ticket."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

from sqlalchemy.orm import sessionmaker

from app.models.support import Ticket
from app.schemas.support import TicketCreate
from app.services import support as support_service
from app.services.owner_commands import CommandContext


def test_concurrent_customer_submission_replays_one_ticket(engine, subscriber) -> None:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    key = uuid4()
    barrier = Barrier(2)

    def submit() -> UUID:
        with factory() as worker:
            command = support_service.CustomerTicketSubmissionCommand(
                context=CommandContext.system(
                    actor=f"subscriber:{subscriber.id}",
                    scope="support.ticket:customer_submission",
                    reason="Concurrent customer retry test",
                    idempotency_key=str(key),
                ),
                payload=TicketCreate(
                    title="Concurrent customer issue",
                    description="Same request",
                    subscriber_id=subscriber.id,
                    description_is_internal=False,
                ),
                submission_id=key,
                source=support_service.CustomerTicketSubmissionSource.mobile_api,
            )
            barrier.wait(timeout=10)
            return support_service.Tickets.create_customer_submission(
                worker, command=command
            ).ticket.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ticket_ids = list(pool.map(lambda _: submit(), range(2)))

    assert len(set(ticket_ids)) == 1
    with factory() as check:
        assert (
            check.query(Ticket)
            .filter(
                Ticket.subscriber_id == subscriber.id,
                Ticket.customer_submission_id == key,
            )
            .count()
            == 1
        )
