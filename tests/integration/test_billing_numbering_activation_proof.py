"""Hosted-CI only: real migrated PostgreSQL read-only candidate observation."""

from collections.abc import Callable
from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest
from dotmac_kernel.settings_cache import active_settings_cache, install_settings_cache
from dotmac_numbering import SeriesConfiguration
from sqlalchemy import text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app import db as app_db
from app.models.billing import Invoice, InvoiceStatus
from app.models.subscriber import Subscriber
from app.services.billing_numbering_activation_proof import (
    ActivationProofQuery,
    billing_numbering_activation_proof_report,
)
from app.services.billing_numbering_readiness import NumberingReadinessError
from app.services.subscriber import _default_reseller_id
from scripts.migration import billing_numbering_activation_proof as cli

pytestmark = pytest.mark.integration
PROPOSAL = SeriesConfiguration(
    series_code="invoice_number",
    prefix="CANDIDATE",
    suffix="",
    separator="-",
    min_digits=6,
    include_year=False,
    year_digits=4,
    include_month=False,
    reset_policy="never",
    start_value=1,
)
QUERY = ActivationProofQuery(reference_date=date(2026, 10, 14), proposal=PROPOSAL)


@pytest.fixture()
def factory(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> sessionmaker[Session]:
    if engine.dialect.name != "postgresql":
        pytest.fail("candidate proof requires real migrated PostgreSQL")
    result = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(app_db, "SessionLocal", result)
    return result


def catalogue(db: Session) -> tuple[int, ...]:
    return tuple(
        db.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()  # noqa: S608 - fixed catalogue
        for table in (
            "public.document_sequences",
            "mod_numbering.number_series",
            "mod_numbering.series_counters",
            "mod_numbering.allocation_receipts",
            "mod_numbering.series_repairs",
            "public.idempotency_records",
        )
    )  # noqa: S608 - fixed catalogue


def test_cli_has_no_state_effects(
    factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    with app_db.read_only_snapshot_session() as db:
        before = catalogue(db)
    previous = active_settings_cache()
    args = [
        "--reference-date",
        "2026-10-14",
        "--series-code",
        "invoice_number",
        "--prefix",
        "CANDIDATE",
        "--suffix",
        "",
        "--separator",
        "-",
        "--min-digits",
        "6",
        "--include-year",
        "false",
        "--year-digits",
        "4",
        "--include-month",
        "false",
        "--reset-policy",
        "never",
        "--start-value",
        "1",
    ]
    status = cli.main(args)
    assert active_settings_cache() is previous
    payload = json.loads(capsys.readouterr().out)
    assert status in (0, 1) and payload["complete"] and not payload["activatable"]
    with app_db.read_only_snapshot_session() as db:
        assert catalogue(db) == before


def test_full_history_collision_and_independent_writer_snapshot(
    factory: sessionmaker[Session],
) -> None:
    marker = uuid4().hex[:12]
    proposal = replace(PROPOSAL, prefix=f"CANDIDATE-{marker}")
    query = replace(QUERY, proposal=proposal)
    with factory() as writer:
        subscriber = Subscriber(
            first_name="Candidate",
            last_name="Proof",
            email=f"proof-{marker}@example.invalid",
            reseller_id=_default_reseller_id(writer),
        )
        writer.add(subscriber)
        writer.flush()
        invoice = Invoice(
            account_id=subscriber.id,
            invoice_number=f"CANDIDATE-{marker}-000001",
            status=InvoiceStatus.void,
            is_active=False,
        )
        writer.add(invoice)
        writer.commit()
        invoice_id = invoice.id
    previous = active_settings_cache()
    install_settings_cache(None)
    try:
        with app_db.read_only_snapshot_session() as db:
            first = billing_numbering_activation_proof_report(db, query=query)
            assert first.target_matches >= 1 and not first.candidate_proven
            assert "historical_target_collision" in first.blockers
            with factory() as writer:
                invoice = writer.get(Invoice, invoice_id)
                assert invoice is not None
                invoice.invoice_number = f"CHANGED-{marker}"
                writer.commit()
            second = billing_numbering_activation_proof_report(db, query=query)
            assert second.evidence_digest == first.evidence_digest
        with app_db.read_only_snapshot_session() as db:
            fresh = billing_numbering_activation_proof_report(db, query=query)
            assert fresh.evidence_digest != first.evidence_digest
    finally:
        install_settings_cache(previous)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE public.document_sequences SET next_value=next_value+1",
        "UPDATE mod_numbering.series_counters SET next_value=next_value+1",
        "DELETE FROM mod_numbering.number_series WHERE false",
        "INSERT INTO public.document_sequences (key,next_value) VALUES ('candidate-forbidden',1)",
    ],
)
def test_read_only_snapshot_forbids_writes(
    factory: sessionmaker[Session], statement: str
) -> None:
    with app_db.read_only_snapshot_session() as db:
        assert db.scalar(text("SHOW transaction_read_only")) == "on"
        assert db.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        with pytest.raises(DBAPIError) as excinfo:
            db.execute(text(statement))
        assert "read-only" in str(excinfo.value).lower()


def test_query_refuses_writable_and_dirty_sessions(
    factory: sessionmaker[Session],
) -> None:
    with factory() as db:
        with pytest.raises(NumberingReadinessError, match="unsafe_snapshot"):
            billing_numbering_activation_proof_report(db, query=QUERY)
    with app_db.read_only_snapshot_session() as db:
        db.add(Invoice(invoice_number="pending"))
        with pytest.raises(NumberingReadinessError, match="unsafe_snapshot"):
            billing_numbering_activation_proof_report(db, query=QUERY)


def test_actual_module_fields_remain_bound_to_one_snapshot(
    cloned_database: Callable[[str], URL], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Private migrated clone permits deliberate fixture drift without shared state."""
    from dotmac_numbering import (
        AllocationReceipt,
        NumberSeries,
        SeriesCounter,
        configure_series,
    )
    from sqlalchemy import create_engine, select

    from app.services.billing_numbering_readiness import SCOPE

    isolated = create_engine(cloned_database("heads"))
    sessions = sessionmaker(bind=isolated, autoflush=False)
    monkeypatch.setattr(app_db, "SessionLocal", sessions)
    previous = active_settings_cache()
    install_settings_cache(None)
    try:
        with sessions() as writer:
            configure_series(writer, scope=SCOPE, configuration=PROPOSAL)
            writer.add(
                SeriesCounter(
                    tenant_id=SCOPE.tenant_id,
                    series_code=PROPOSAL.series_code,
                    period_key="*",
                    next_value=1,
                )
            )
            writer.commit()
        with app_db.read_only_snapshot_session() as db:
            # Existing migrated online role is constrained by the read-only snapshot.
            db.execute(text("SET LOCAL ROLE app_user"))
            assert db.scalar(text("SELECT current_user")) == "app_user"
            assert (
                db.scalar(
                    text(
                        "SELECT has_table_privilege(current_user, 'mod_numbering.number_series', 'SELECT')"
                    )
                )
                is True
            )
            first = billing_numbering_activation_proof_report(db, query=QUERY)
            assert first.actual_preview_verified
            before = catalogue(db)
            with sessions() as writer:
                series = writer.scalar(
                    select(NumberSeries).where(
                        NumberSeries.tenant_id == SCOPE.tenant_id,
                        NumberSeries.series_code == PROPOSAL.series_code,
                    )
                )
                assert series is not None
                series.suffix = "CHANGED"
                writer.commit()
            assert (
                billing_numbering_activation_proof_report(
                    db, query=QUERY
                ).evidence_digest
                == first.evidence_digest
            )
            with sessions() as writer:
                counter = writer.scalar(
                    select(SeriesCounter).where(
                        SeriesCounter.tenant_id == SCOPE.tenant_id,
                        SeriesCounter.series_code == PROPOSAL.series_code,
                    )
                )
                assert counter is not None
                counter.next_value = 2
                writer.commit()
            assert (
                billing_numbering_activation_proof_report(
                    db, query=QUERY
                ).evidence_digest
                == first.evidence_digest
            )
            with sessions() as writer:
                writer.add(
                    AllocationReceipt(
                        tenant_id=SCOPE.tenant_id,
                        series_code=PROPOSAL.series_code,
                        period_key="*",
                        allocated_value=1,
                        formatted_number="CANDIDATE-000001-CHANGED",
                        reference_date=QUERY.reference_date,
                        idempotency_key="synthetic-unlinked-fixture",
                        allocated_by="candidate-test",
                    )
                )
                writer.commit()
            assert (
                billing_numbering_activation_proof_report(
                    db, query=QUERY
                ).evidence_digest
                == first.evidence_digest
            )
            assert catalogue(db) == before
        with app_db.read_only_snapshot_session() as db:
            db.execute(text("SET LOCAL ROLE app_user"))
            fresh = billing_numbering_activation_proof_report(db, query=QUERY)
            assert fresh.evidence_digest != first.evidence_digest
            assert not fresh.actual_preview_verified and not fresh.candidate_proven
            assert "target_configuration_mismatch" in fresh.blockers
            assert "existing_target_history_requires_reconciliation" in fresh.blockers
    finally:
        install_settings_cache(previous)
        isolated.dispose()
