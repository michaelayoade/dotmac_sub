"""PostgreSQL proof that concurrent customer retries issue one Ticket."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

from app.models.subscriber import Subscriber
from app.models.support import Ticket
from app.schemas.support import TicketCreate
from app.services import support as support_service
from app.services.owner_commands import CommandContext
from app.services.subscriber import _default_reseller_id


def test_concurrent_customer_submission_replays_one_ticket(
    cloned_database: Callable[[str], URL],
) -> None:
    engine = create_engine(cloned_database("heads"))
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    key = uuid4()
    barrier = Barrier(2)
    with factory() as setup:
        subscriber = Subscriber(
            first_name="Concurrent",
            last_name="Customer",
            email=f"ticket-retry-{uuid4()}@example.com",
            reseller_id=_default_reseller_id(setup),
        )
        setup.add(subscriber)
        setup.commit()
        subscriber_id = subscriber.id

    def submit() -> UUID:
        with factory() as worker:
            command = support_service.CustomerTicketSubmissionCommand(
                context=CommandContext.system(
                    actor=f"subscriber:{subscriber_id}",
                    scope="support.ticket:customer_submission",
                    reason="Concurrent customer retry test",
                    idempotency_key=str(key),
                ),
                payload=TicketCreate(
                    title="Concurrent customer issue",
                    description="Same request",
                    subscriber_id=subscriber_id,
                    description_is_internal=False,
                ),
                submission_id=key,
                source=support_service.CustomerTicketSubmissionSource.mobile_api,
            )
            barrier.wait(timeout=10)
            return support_service.Tickets.create_customer_submission(
                worker, command=command
            ).ticket.id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            ticket_ids = list(pool.map(lambda _: submit(), range(2)))

        assert len(set(ticket_ids)) == 1
        with factory() as check:
            assert (
                check.query(Ticket)
                .filter(
                    Ticket.subscriber_id == subscriber_id,
                    Ticket.customer_submission_id == key,
                )
                .count()
                == 1
            )
    finally:
        engine.dispose()
