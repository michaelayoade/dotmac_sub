"""Real PG lock-timeout and 661-to-662 proof; no hand-created schema."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from alembic import command
from scripts.migration import regional_report_billing_indexes as contract

ROOT = Path(__file__).resolve().parents[2]
PREDECESSOR = "661_support_ticket_comment_idempotency"
REPAIR = "662_validate_regional_report_billing_indexes"


def _upgrade(revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, revision)


def test_fresh_migrated_head_has_both_exact_reporting_indexes(
    db_session: Session,
) -> None:
    contract.validate_postgres_indexes(db_session.connection())


@pytest.mark.parametrize("spec", contract.INDEX_SPECS)
def test_forward_migration_repairs_real_timeout_without_rebuilding_other_index(
    cloned_database: Callable[[str], URL],
    spec: contract.IndexSpec,
) -> None:
    url = cloned_database(PREDECESSOR)
    engine = sa.create_engine(url)
    other = next(candidate for candidate in contract.INDEX_SPECS if candidate != spec)
    try:
        with engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as catalog:
            other_oid = catalog.scalar(
                sa.text("SELECT to_regclass(:name)::oid"),
                {"name": f"public.{other.name}"},
            )
            counts = catalog.execute(
                sa.text(
                    "SELECT (SELECT count(*) FROM invoices), (SELECT count(*) FROM payments)"
                )
            ).one()
            catalog.exec_driver_sql(spec.drop_sql)
            with engine.connect() as blocker:
                # This compatible writer lock permits the initial catalog
                # entry, but delays the concurrent build's first table scan.
                # No customer rows or system catalogs are modified by the test.
                blocker.exec_driver_sql(
                    f"LOCK TABLE public.{spec.table} IN ROW EXCLUSIVE MODE"
                )
                catalog.exec_driver_sql("SET lock_timeout = '200ms'")
                with pytest.raises(sa.exc.OperationalError) as failure:
                    catalog.exec_driver_sql(spec.create_sql)
                assert getattr(failure.value.orig, "sqlstate", None) == "55P03"
                state = contract.postgres_index_state(catalog, spec=spec)
                assert state is not None and not state.valid
                blocker.rollback()
            catalog.exec_driver_sql("RESET lock_timeout")
            # Reproduce the old retry: it succeeds but retains the invalid object.
            catalog.exec_driver_sql(
                spec.create_sql.replace("CONCURRENTLY", "CONCURRENTLY IF NOT EXISTS")
            )
            state = contract.postgres_index_state(catalog, spec=spec)
            assert state is not None and not state.valid
            assert (
                catalog.scalar(
                    sa.text(
                        "SELECT count(*) FROM alembic_version WHERE version_num=:revision"
                    ),
                    {"revision": PREDECESSOR},
                )
                == 1
            )

        # A real predecessor-to-repair Alembic upgrade, not direct helper calls
        # or manually stamped current metadata.
        _upgrade(REPAIR)
        with engine.connect() as catalog:
            contract.validate_postgres_indexes(catalog)
            assert (
                catalog.scalar(
                    sa.text("SELECT to_regclass(:name)::oid"),
                    {"name": f"public.{other.name}"},
                )
                == other_oid
            )
            assert (
                catalog.execute(
                    sa.text(
                        "SELECT (SELECT count(*) FROM invoices), (SELECT count(*) FROM payments)"
                    )
                ).one()
                == counts
            )
            assert (
                catalog.scalar(
                    sa.text(
                        "SELECT count(*) FROM alembic_version WHERE version_num=:revision"
                    ),
                    {"revision": REPAIR},
                )
                == 1
            )
            oids = tuple(
                catalog.scalar(
                    sa.text("SELECT to_regclass(:name)::oid"),
                    {"name": f"public.{item.name}"},
                )
                for item in contract.INDEX_SPECS
            )
        _upgrade(REPAIR)
        with engine.connect() as catalog:
            assert (
                tuple(
                    catalog.scalar(
                        sa.text("SELECT to_regclass(:name)::oid"),
                        {"name": f"public.{item.name}"},
                    )
                    for item in contract.INDEX_SPECS
                )
                == oids
            )
    finally:
        engine.dispose()


def test_forward_migration_restores_missing_indexes_and_downgrade_preserves_them(
    cloned_database: Callable[[str], URL],
) -> None:
    url = cloned_database(PREDECESSOR)
    engine = sa.create_engine(url)
    try:
        with engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as catalog:
            for spec in contract.INDEX_SPECS:
                catalog.exec_driver_sql(spec.drop_sql)
        _upgrade(REPAIR)
        with engine.connect() as catalog:
            contract.validate_postgres_indexes(catalog)
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "alembic"))
        command.downgrade(config, PREDECESSOR)
        with engine.connect() as catalog:
            contract.validate_postgres_indexes(catalog)
    finally:
        engine.dispose()
