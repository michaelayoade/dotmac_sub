"""Hosted PostgreSQL proof that invoice numbering stays on the legacy sequence."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from alembic import command
from app.models.domain_settings import SettingDomain
from app.services import settings_spec
from app.services.billing.invoices import next_invoice_number
from tests.integration import test_numbering_module_composition as composition

isolated_database = composition.isolated_database


def test_invoice_numbering_remains_legacy_until_cutover_is_approved(
    isolated_database: URL,
) -> None:
    command.upgrade(composition._config(isolated_database), "heads")
    engine = sa.create_engine(isolated_database)
    try:
        with engine.begin() as connection:
            with Session(bind=connection) as db:
                start = settings_spec.resolve_value(
                    db, SettingDomain.billing, "invoice_number_start"
                )
                first = next_invoice_number(db)
                second = next_invoice_number(db)

            assert first != second
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT next_value FROM public.document_sequences "
                        "WHERE key='invoice_number'"
                    )
                )
                == start + 2
            )
            assert (
                connection.scalar(
                    sa.text("SELECT count(*) FROM mod_numbering.number_series")
                )
                == 0
            )
            assert (
                connection.scalar(
                    sa.text("SELECT count(*) FROM mod_numbering.series_counters")
                )
                == 0
            )
            assert (
                connection.scalar(
                    sa.text("SELECT count(*) FROM mod_numbering.allocation_receipts")
                )
                == 0
            )
    finally:
        engine.dispose()
