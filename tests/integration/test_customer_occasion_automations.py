"""Acceptance requires PostgreSQL/PostGIS migrated by the real Alembic chain."""

from collections.abc import Callable

from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from alembic import command
from app.models.customer_occasion import CustomerOccasionAutomation


def test_migration_seeds_review_only_configurations(db_session: Session) -> None:
    assert db_session.get_bind().dialect.name == "postgresql"
    rows = db_session.scalars(select(CustomerOccasionAutomation)).all()
    assert {row.occasion for row in rows} == {"birthday", "anniversary"}
    assert all(not row.enabled for row in rows)
    assert all(row.revision == 1 and row.timezone == "Africa/Lagos" for row in rows)
    checks = inspect(db_session.get_bind()).get_check_constraints(
        "customer_occasion_automations"
    )
    assert {row["name"] for row in checks} >= {
        "ck_occasion_kind",
        "ck_occasion_hour",
        "ck_occasion_minute",
        "ck_occasion_revision",
    }


def test_real_predecessor_creates_disabled_configuration(
    cloned_database: Callable[[str], URL],
) -> None:
    target = cloned_database("690_backdated_invoice_permissions")
    engine = create_engine(target)
    try:
        with engine.begin() as connection:
            # The historical squash imports current models. Remove its empty
            # future table to rehearse a real deployment's predecessor shape.
            if inspect(connection).has_table("customer_occasion_automations"):
                assert (
                    connection.scalar(
                        text("SELECT count(*) FROM customer_occasion_automations")
                    )
                    == 0
                )
                connection.execute(text("DROP TABLE customer_occasion_automations"))
        command.upgrade(Config("alembic.ini"), "heads")
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT occasion, enabled FROM customer_occasion_automations ORDER BY occasion"
                )
            ).all()
            assert rows == [("anniversary", False), ("birthday", False)]
            keys = connection.scalars(
                text(
                    "SELECT key FROM permissions WHERE key LIKE 'notification:occasion:%' "
                )
            ).all()
            assert set(keys) == {
                "notification:occasion:read",
                "notification:occasion:update",
                "notification:occasion:test",
            }
    finally:
        engine.dispose()
