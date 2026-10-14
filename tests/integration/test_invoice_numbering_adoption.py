"""Hosted CI only: adopted participant on the genuine migrated PG lineage."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from dotmac_kernel.cache import TenantScope
from dotmac_numbering.models import AllocationReceipt, SeriesCounter
from dotmac_numbering.service import (
    MAX_VALUE,
    NumberingError,
    SeriesConfiguration,
    configure_series,
)
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session

from alembic import command
from app.models.domain_settings import SettingDomain
from app.services import settings_spec
from app.services.billing.invoices import (
    InvoiceNumberPurpose,
    InvoiceOwnerError,
    next_invoice_number,
)
from app.services.operator_tenant import OPERATOR_TENANT_ID
from tests.integration import test_numbering_module_composition as composition

isolated_database = composition.isolated_database
REFERENCE_DATE = date(2026, 10, 13)


def _number(db: Session, invoice_id: UUID, *, conversion: bool = False) -> str:
    return next_invoice_number(
        db,
        invoice_id=invoice_id,
        reference_date=REFERENCE_DATE,
        purpose=(
            InvoiceNumberPurpose.PROFORMA_CONVERSION
            if conversion
            else InvoiceNumberPurpose.INITIAL
        ),
        conversion_key="a" * 64 if conversion else None,
    )


def _counts(engine: Engine) -> tuple[int, int, int]:
    with engine.begin() as connection:
        composition._scope(connection, OPERATOR_TENANT_ID)
        return (
            connection.scalar(
                sa.text("SELECT count(*) FROM mod_numbering.allocation_receipts")
            ),
            connection.scalar(
                sa.text(
                    "SELECT next_value FROM mod_numbering.series_counters WHERE series_code='invoice_number' AND period_key='*'"
                )
            ),
            connection.scalar(
                sa.text(
                    "SELECT next_value FROM public.document_sequences WHERE key='invoice_number'"
                )
            ),
        )


def test_adopted_numbering_replay_conversion_rollback_concurrency_and_incumbent(
    isolated_database: URL,
) -> None:
    command.upgrade(composition._config(isolated_database), "heads")
    engine = sa.create_engine(isolated_database)
    invoice_id = uuid4()
    try:
        with engine.begin() as connection:
            # Synthetic incumbent evidence; adoption may never advance it.
            connection.execute(
                sa.text(
                    "INSERT INTO public.document_sequences (id, key, next_value, created_at, updated_at) "
                    "VALUES (:id, 'invoice_number', 900, now(), now())"
                ),
                {"id": uuid4()},
            )
            with Session(bind=connection) as db:
                with pytest.raises(InvoiceOwnerError) as absent_series:
                    _number(db, invoice_id)
                assert absent_series.value.code.endswith("series_unready")
                assert (
                    connection.scalar(
                        sa.text(
                            "SELECT count(*) FROM mod_numbering.allocation_receipts"
                        )
                    )
                    == 0
                )
                prefix = settings_spec.resolve_value(
                    db, SettingDomain.billing, "invoice_number_prefix"
                )
                padding = settings_spec.resolve_value(
                    db, SettingDomain.billing, "invoice_number_padding"
                )
                start = settings_spec.resolve_value(
                    db, SettingDomain.billing, "invoice_number_start"
                )
                configure_series(
                    db,
                    scope=TenantScope(tenant_id=OPERATOR_TENANT_ID),
                    configuration=SeriesConfiguration(
                        series_code="invoice_number",
                        prefix=prefix,
                        separator="",
                        min_digits=max(1, padding),
                        start_value=start,
                    ),
                )
                with pytest.raises(InvoiceOwnerError) as absent_counter:
                    _number(db, invoice_id)
                assert absent_counter.value.code.endswith("counter_unready")
                assert (
                    connection.scalar(
                        sa.text("SELECT count(*) FROM mod_numbering.series_counters")
                    )
                    == 0
                )
                # Seeding is explicit fixture preparation, never a product side effect.
                db.add(
                    SeriesCounter(
                        tenant_id=OPERATOR_TENANT_ID,
                        series_code="invoice_number",
                        period_key="*",
                        next_value=start,
                    )
                )
                db.flush()
        initial = _counts(engine)
        assert initial[0] == 0 and initial[2] == 900
        # Participant work remains inside the caller's transaction.
        with engine.connect() as connection:
            transaction = connection.begin()
            with Session(bind=connection) as db:
                rolled_back = _number(db, invoice_id)
            transaction.rollback()
        assert _counts(engine) == initial
        with engine.begin() as connection:
            with Session(bind=connection) as db:
                first = _number(db, invoice_id)
                assert first == rolled_back == _number(db, invoice_id)
                converted = _number(db, invoice_id, conversion=True)
                assert converted != first
                assert converted == _number(db, invoice_id, conversion=True)
        assert _counts(engine) == (2, initial[1] + 2, 900)

        # Two independent caller-owned transactions racing one identity converge.
        barrier = Barrier(2)
        racing_id = uuid4()

        def caller() -> str:
            with engine.begin() as connection:
                with Session(bind=connection) as db:
                    barrier.wait(timeout=15)
                    return _number(db, racing_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: caller(), range(2)))
        assert results[0] == results[1]
        assert results[0] not in {first, converted}
        assert _counts(engine) == (3, initial[1] + 3, 900)

        # Explicit fixture preparation of the final available value. Keep the
        # series start configuration unchanged so the real compatibility guard
        # still runs, and exercise the actual receipt/idempotency owner.
        terminal_id = uuid4()
        terminal_key = f"billing:invoice:{terminal_id}:initial"
        with engine.begin() as connection:
            with Session(bind=connection) as db:
                counter = db.scalar(
                    sa.select(SeriesCounter)
                    .where(
                        SeriesCounter.tenant_id == OPERATOR_TENANT_ID,
                        SeriesCounter.series_code == "invoice_number",
                        SeriesCounter.period_key == "*",
                    )
                    .with_for_update()
                )
                assert counter is not None
                counter.next_value = MAX_VALUE - 1
                db.flush()
                last_number = _number(db, terminal_id)
                receipt = db.execute(
                    sa.select(
                        AllocationReceipt.id,
                        AllocationReceipt.allocated_value,
                        AllocationReceipt.formatted_number,
                    ).where(
                        AllocationReceipt.tenant_id == OPERATOR_TENANT_ID,
                        AllocationReceipt.idempotency_key == terminal_key,
                    )
                ).one()
                assert receipt.allocated_value == MAX_VALUE - 1
                assert receipt.formatted_number == last_number
        terminal_counts = (4, MAX_VALUE, 900)
        assert _counts(engine) == terminal_counts
        with engine.begin() as connection:
            with Session(bind=connection) as db:
                assert _number(db, terminal_id) == last_number
                replay_receipt = db.execute(
                    sa.select(
                        AllocationReceipt.id,
                        AllocationReceipt.allocated_value,
                        AllocationReceipt.formatted_number,
                    ).where(
                        AllocationReceipt.tenant_id == OPERATOR_TENANT_ID,
                        AllocationReceipt.idempotency_key == terminal_key,
                    )
                ).one()
                assert replay_receipt == receipt
        assert _counts(engine) == terminal_counts
        # Catch outside the caller transaction so the exhausted new request's
        # reservation rolls back too; the existing terminal receipt survives.
        with pytest.raises(NumberingError) as exhausted:
            with engine.begin() as connection:
                with Session(bind=connection) as db:
                    _number(db, uuid4())
        assert exhausted.value.code == "numbering.series_exhausted"
        assert _counts(engine) == terminal_counts

    finally:
        engine.dispose()
