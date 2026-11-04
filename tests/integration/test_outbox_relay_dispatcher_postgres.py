"""PostgreSQL proof that the dispatcher drives the real claim/settle functions.

Runs inside a rolled-back outer transaction (the Session joins it with a
savepoint), seeding rows as the migrated owner. The EXECUTE-only privilege
posture of ``outbox_dispatcher`` is proven by
``test_outbox_relay_provider_postgres.py``.
"""

from __future__ import annotations

import sqlalchemy as sa
from dotmac_kernel.messaging.relay import ClaimedEvent, RelayPolicy
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.outbox_relay.dispatcher import run_cycle
from app.outbox_relay.registry import OutboxHandlerRegistry

TENANT = "8c7ae830-51fc-52ae-9818-d84b2a35e568"
PARKED = "51111111-1111-4111-8111-111111111111"
SENT = "52222222-2222-4222-8222-222222222222"
DEAD = "53333333-3333-4333-8333-333333333333"


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
    db_session: Session,
) -> None:
    connection = db_session.connection()
    # The shared test database can hold committed outbox rows from other tests.
    # Make them unclaimable for THIS transaction only (db_session rolls it
    # back): push pending rows out of the window and freshen stale leases. Rows
    # are neither deleted nor changed beyond the transaction.
    connection.execute(
        sa.text(
            "UPDATE public.outbox_events SET "
            "available_at = CASE WHEN status = 'pending' "
            "THEN now() + interval '1 day' ELSE available_at END, "
            "leased_at = CASE WHEN status = 'claimed' THEN now() ELSE leased_at END "
            "WHERE status IN ('pending', 'claimed')"
        )
    )
    assert (
        connection.execute(
            sa.text(
                "SELECT count(*) FROM public.outbox_events "
                "WHERE (status = 'pending' AND available_at <= now()) "
                "OR (status = 'claimed' AND leased_at < now() - interval '300 seconds')"
            )
        ).scalar()
        == 0
    )
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

    result = run_cycle(
        db_session,
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

    backlog = read_outbox_backlog(db_session)
    assert (backlog.pending, backlog.dead) == (1, 1)
    assert backlog.oldest_pending_age_seconds >= 0.0
    assert _status(connection, DEAD) == ("dead", 2, "RuntimeError")


OTHER_TENANT_SCOPE = "33333333-3333-4333-8333-333333333333"


def test_backlog_read_sees_operator_rows_under_the_runtime_role(
    db_session: Session,
) -> None:
    """Runs as ``app_user`` (RLS applies, no SELECT on ``public.tenants``).

    The transaction scope is first set to a foreign tenant, so a read that did
    not install the operator scope itself would see zero rows."""
    from app.services.outbox_backlog import read_outbox_backlog

    connection = db_session.connection()
    connection.execute(
        sa.text(
            "INSERT INTO public.outbox_events "
            "(id, tenant_id, event_type, payload, status, created_at) VALUES "
            "(:a, :t1, 'canary.a', '{}', 'pending', now() - interval '1 hour'), "
            "(:b, :t1, 'canary.b', '{}', 'pending', now() - interval '2 hours'), "
            "(:c, :t1, 'canary.c', '{}', 'dead', now())"
        ),
        {
            "a": "61111111-1111-4111-8111-111111111111",
            "b": "62222222-2222-4222-8222-222222222222",
            "c": "63333333-3333-4333-8333-333333333333",
            "t1": TENANT,
        },
    )
    connection.execute(sa.text("SET LOCAL ROLE app_user"))
    connection.execute(
        sa.text("SELECT set_config('app.current_tenant', :t, true)"),
        {"t": OTHER_TENANT_SCOPE},
    )
    # Premise: the runtime role is genuinely scoped (a foreign scope sees none).
    visible = connection.execute(
        sa.text(
            "SELECT count(*) FROM public.outbox_events WHERE event_type LIKE 'canary.%'"
        )
    ).scalar()
    assert visible == 0

    # The Session's transaction is already begun, so the root after_begin hook
    # cannot restore the operator scope: only read_outbox_backlog's own scoping
    # can. Deleting it makes the read see 0 rows and this test fail.
    backlog = read_outbox_backlog(db_session)
    assert backlog.pending >= 2 and backlog.dead >= 1
    assert backlog.oldest_pending_age_seconds >= 2 * 3600 - 60
