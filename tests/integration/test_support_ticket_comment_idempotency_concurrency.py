"""PostgreSQL serialization proof for duplicate Ticket comment submissions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

from sqlalchemy.orm import sessionmaker

from app.models.support import TicketComment
from app.schemas.support import TicketCommentCreate, TicketCreate
from app.services import support as support_service


def test_concurrent_duplicate_comment_submissions_return_one_comment(engine) -> None:
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with session_factory() as setup:
        ticket = support_service.tickets.create(
            setup,
            TicketCreate(title=f"Concurrent comment {uuid4().hex}"),
            actor_id=None,
        )
        ticket_id = ticket.id

    idempotency_key = uuid4()
    barrier = Barrier(2)

    def submit() -> UUID:
        with session_factory() as worker:
            barrier.wait(timeout=10)
            comment = support_service.tickets.create_comment(
                worker,
                str(ticket_id),
                TicketCommentCreate(
                    body="One concurrent note",
                    is_internal=True,
                    idempotency_key=idempotency_key,
                ),
                actor_id=None,
            )
            return comment.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        comment_ids = list(pool.map(lambda _index: submit(), range(2)))

    assert len(set(comment_ids)) == 1
    with session_factory() as check:
        assert (
            check.query(TicketComment)
            .filter(
                TicketComment.ticket_id == ticket_id,
                TicketComment.idempotency_key == idempotency_key,
            )
            .count()
            == 1
        )
