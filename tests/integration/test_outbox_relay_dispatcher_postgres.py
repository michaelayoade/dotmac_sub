"""PostgreSQL proof that the dispatcher drives the real claim/settle functions.

Runs inside a rolled-back outer transaction (the Session joins it with a
savepoint), seeding rows as the migrated owner. The EXECUTE-only privilege
posture of ``outbox_dispatcher`` is proven by
``test_outbox_relay_provider_postgres.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from dotmac_kernel.messaging.relay import ClaimedEvent, RelayPolicy
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.outbox_relay.dispatcher import run_cycle
from app.outbox_relay.registry import OutboxHandlerRegistry

TENANT = "8c7ae830-51fc-52ae-9818-d84b2a35e568"
PARKED = "51111111-1111-4111-8111-111111111111"
SENT = "52222222-2222-4222-8222-222222222222"
DEAD = "53333333-3333-4333-8333-333333333333"


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as conn, conn.begin() as outer:
        try:
            yield conn
        finally:
            outer.rollback()


def _status(conn: Connection, event_id: str) -> tuple[str, int, str | None]:
    row = conn.execute(
        sa.text(
            "SELECT status, attempts, last_error FROM public.outbox_events "
            "WHERE id = :id"
        ),
        {"id": event_id},
    ).one()
    return str(row[0]), int(row[1]), row[2]


def test_park_deliver_and_dead_letter_against_the_real_functions(
    connection: Connection,
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO public.outbox_events "
            "(id, tenant_id, event_type, payload, attempts) VALUES "
            "(:p, :t, 'canary.unregistered', '{}', 7), "
            "(:s, :t, 'canary.ok', '{}', 0), "
            "(:d, :t, 'canary.boom', '{}', 1)"
        ),
        {"p": PARKED, "s": SENT, "d": DEAD, "t": TENANT},
    )
    registry = OutboxHandlerRegistry()
    registry.register("canary.ok", lambda event, tenant_db: None)

    def boom(event: ClaimedEvent, tenant_db: Session) -> None:
        raise RuntimeError("private")

    registry.register("canary.boom", boom)

    db = Session(bind=connection, join_transaction_mode="create_savepoint")
    result = run_cycle(
        db,
        registry=registry,
        policy=RelayPolicy(max_attempts=2),
        worker_id="canary-worker",
        park_seconds=600,
        tenant_session_factory=lambda: Session(
            bind=connection, join_transaction_mode="create_savepoint"
        ),
    )
    assert (result.claimed, result.succeeded, result.parked, result.dead_lettered) == (
        3,
        1,
        1,
        1,
    )

    # Unregistered: back to pending, attempts untouched (7 > max_attempts), parked.
    assert _status(connection, PARKED)[:2] == ("pending", 7)
    assert connection.execute(
        sa.text("SELECT available_at > now() FROM public.outbox_events WHERE id = :i"),
        {"i": PARKED},
    ).scalar()
    assert _status(connection, SENT)[0] == "sent"
    from app.services.outbox_backlog import read_outbox_backlog

    backlog = read_outbox_backlog(Session(bind=connection))
    assert (backlog.pending, backlog.dead) == (1, 1)
    assert backlog.oldest_pending_age_seconds >= 0.0
    assert _status(connection, DEAD) == ("dead", 2, "RuntimeError")


SECOND_TENANT = "33333333-3333-4333-8333-333333333333"


def test_backlog_read_sees_every_tenant_under_the_runtime_role(
    connection: Connection,
) -> None:
    """Runs as ``app_user`` (RLS applies), not the owner: an unscoped read would
    see one tenant or none. Two tenants carry rows; the backlog must count both."""
    from app.services.outbox_backlog import read_outbox_backlog

    connection.execute(
        sa.text(
            "INSERT INTO public.tenants (id, slug, name, is_active) "
            "VALUES (:id, 'backlog-canary', 'Backlog canary', true) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"id": SECOND_TENANT},
    )
    connection.execute(
        sa.text(
            "INSERT INTO public.outbox_events "
            "(id, tenant_id, event_type, payload, status, created_at) VALUES "
            "(:a, :t1, 'canary.a', '{}', 'pending', now() - interval '1 hour'), "
            "(:b, :t2, 'canary.b', '{}', 'pending', now() - interval '2 hours'), "
            "(:c, :t2, 'canary.c', '{}', 'dead', now())"
        ),
        {
            "a": "61111111-1111-4111-8111-111111111111",
            "b": "62222222-2222-4222-8222-222222222222",
            "c": "63333333-3333-4333-8333-333333333333",
            "t1": TENANT,
            "t2": SECOND_TENANT,
        },
    )
    connection.execute(sa.text("SET LOCAL ROLE app_user"))
    connection.execute(
        sa.text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT}
    )
    # Premise: the runtime role is genuinely scoped (sees only its own tenant).
    visible = connection.execute(
        sa.text(
            "SELECT count(*) FROM public.outbox_events WHERE event_type LIKE 'canary.%'"
        )
    ).scalar()
    assert visible == 1

    db = Session(bind=connection, join_transaction_mode="create_savepoint")
    backlog = read_outbox_backlog(db)
    assert backlog.pending >= 2 and backlog.dead >= 1
    assert backlog.oldest_pending_age_seconds >= 2 * 3600 - 60
