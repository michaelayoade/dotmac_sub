"""Migrated PostgreSQL evidence for the fiber coordinator transaction boundary."""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.services.network.fiber_field_verification_job_plans import (
    execute_fiber_field_verification_job_plan,
)
from tests.test_fiber_field_verification_job_plans import (
    _query,
    _ready,
    _stage,
    _subscriber,
    _vendor_target,
)


def test_repeatable_snapshot_and_principal_locks_precede_existing_work_order(
    cloned_database,
):
    # A fresh session on a private migrated clone allows the owner to select the
    # physical isolation level before any SQL, unlike an outer test transaction.
    engine = create_engine(cloned_database("heads"))
    try:
        with Session(engine) as db:
            feature = _stage(db, "FAT-PG-BOUNDARY")
            target, _ = _vendor_target(db)
            query = _query(db, [feature], _subscriber(db), target)
            _, command = _ready(db, query)
            first = execute_fiber_field_verification_job_plan(db, command=command)
            statements: list[tuple[str, str]] = []

            def observe(
                connection, cursor, statement, parameters, context, executemany
            ):
                statements.append((statement.lower(), connection.get_isolation_level()))

            event.listen(engine, "before_cursor_execute", observe)
            try:
                replay = execute_fiber_field_verification_job_plan(db, command=command)
            finally:
                event.remove(engine, "before_cursor_execute", observe)
            assert replay.replayed
            assert (
                replay.assignment_receipt.assignment_generation
                == first.assignment_receipt.assignment_generation
            )
            assert statements and all(
                isolation == "REPEATABLE READ" for _, isolation in statements
            )
            principal_lock = next(
                index
                for index, (sql, _) in enumerate(statements)
                if "system_users" in sql and "for update" in sql
            )
            work_order_lock = next(
                index
                for index, (sql, _) in enumerate(statements)
                if "from work_order " in sql and "for update" in sql
            )
            assert principal_lock < work_order_lock
    finally:
        engine.dispose()
