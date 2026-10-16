"""Hosted-only census acceptance against real Alembic PostgreSQL schema."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app import db as app_db
from app.models.billing import (
    Invoice,
    InvoiceDueDateBasis,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentStatus,
)
from app.models.catalog import (
    AccessType,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
)
from app.models.subscriber import Subscriber
from app.services.billing.opening_export import (
    BillingOpeningExportError,
    preview_billing_opening_export,
)
from app.services.billing.opening_export_contracts import (
    ExceptionCode,
    PreviewBillingOpeningExportQuery,
    SourceKind,
)
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.subscriber import _default_reseller_id

pytestmark = pytest.mark.integration


@pytest.fixture()
def census_factory(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> sessionmaker[Session]:
    if engine.dialect.name != "postgresql":
        pytest.fail("Opening census requires migrated PostgreSQL")
    factory = sessionmaker(bind=engine, autoflush=True, autocommit=False)
    monkeypatch.setattr(app_db, "SessionLocal", factory)
    return factory


@dataclass(frozen=True)
class SeededCensus:
    subscriber_id: UUID
    invoices: tuple[UUID, ...]
    subscriptions: tuple[UUID, ...]
    orphan_payment_id: UUID
    draft_id: UUID
    cutoff: datetime


@pytest.fixture()
def census_rows(census_factory: sessionmaker[Session]) -> SeededCensus:
    with census_factory() as writer:
        marker = uuid4().hex
        account = Subscriber(
            first_name="Opening",
            last_name="Census",
            email=f"census-{marker}@example.invalid",
            reseller_id=_default_reseller_id(writer),
        )
        offer = CatalogOffer(
            name=f"Census {marker}",
            code=f"CEN-{marker}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        )
        writer.add_all([account, offer])
        writer.flush()
        subscriptions = [
            Subscription(subscriber_id=account.id, offer_id=offer.id) for _ in range(2)
        ]
        writer.add_all(subscriptions)
        writer.flush()
        old = datetime(2020, 1, 1, tzinfo=UTC)
        items = [
            Invoice(
                account_id=account.id,
                invoice_number=f"CENSUS-{marker}-{i}",
                status=InvoiceStatus.issued,
                currency="NGN",
                subtotal=Decimal("100"),
                total=Decimal("100"),
                balance_due=Decimal("20") if i == 0 else Decimal("100"),
                issued_at=old,
                due_at=old + timedelta(days=30),
                due_date_basis=InvoiceDueDateBasis.unknown_unverified,
                created_at=old,
                updated_at=old,
            )
            for i in range(2)
        ]
        draft = Invoice(
            account_id=account.id,
            invoice_number=f"CENSUS-DRAFT-{marker}",
            status=InvoiceStatus.draft,
            total=Decimal("0"),
            balance_due=Decimal("0"),
        )
        payment = Payment(
            account_id=account.id,
            currency="NGN",
            amount=Decimal("100"),
            refunded_amount=Decimal("20"),
            status=PaymentStatus.partially_refunded,
            paid_at=old,
        )
        orphan = Payment(
            account_id=None,
            currency="USD",
            amount=Decimal("40"),
            status=PaymentStatus.succeeded,
        )
        writer.add_all([*items, draft, payment, orphan])
        writer.flush()
        writer.add_all(
            [
                InvoiceLine(
                    invoice_id=items[0].id,
                    subscription_id=sub.id,
                    description="Census line",
                    amount=Decimal("50"),
                    unit_price=Decimal("50"),
                )
                for sub in subscriptions
            ]
        )
        writer.add(
            InvoiceLine(
                invoice_id=items[1].id,
                subscription_id=None,
                description="Unlinked historical line",
                amount=Decimal("100"),
                unit_price=Decimal("100"),
            )
        )
        writer.add(
            PaymentAllocation(
                payment_id=payment.id, invoice_id=items[0].id, amount=Decimal("100")
            )
        )
        writer.flush()
        result = SeededCensus(
            account.id,
            tuple(i.id for i in items),
            tuple(s.id for s in subscriptions),
            orphan.id,
            draft.id,
            writer.execute(text("SELECT clock_timestamp()")).scalar_one(),
        )
        writer.commit()
        return result


def query(cutoff: datetime) -> PreviewBillingOpeningExportQuery:
    return PreviewBillingOpeningExportQuery(
        "a" * 40, UUID(int=123), cutoff, (("NGN", 2), ("USD", 2))
    )


def test_complete_old_multisubscription_unlinked_refund_and_orphan_population(
    census_factory: sessionmaker[Session],
    census_rows: SeededCensus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with app_db.read_only_snapshot_session() as db:
        autoflush = db.autoflush

        def forbidden(*args: object, **kwargs: object) -> None:
            pytest.fail("Census attempted a write or transaction completion")

        with monkeypatch.context() as guard:
            for name in (
                "add",
                "add_all",
                "delete",
                "flush",
                "commit",
                "rollback",
                "begin_nested",
            ):
                guard.setattr(db, name, forbidden)
            result = preview_billing_opening_export(db, query=query(census_rows.cutoff))
        assert db.autoflush is autoflush
        assert db.in_transaction()
        assert db.execute(
            text("SELECT current_setting('app.current_tenant', true)")
        ).scalar_one() == str(OPERATOR_TENANT_ID)
        assert result.capture.schema_heads and result.capture.snapshot_id
        assert result.capture.population_complete
        items = {i.invoice_id: i for i in result.invoices}
        assert set(census_rows.invoices) <= set(items)
        first, second = (items[i] for i in census_rows.invoices)
        assert first.canonical_discharge == Decimal("80")
        assert first.residual == Decimal("20")
        assert second.residual == Decimal("100")
        assert not first.collections_due_basis_verified
        assert set(census_rows.subscriptions) == {
            line.subscription_id
            for line in result.lines
            if line.invoice_id == first.invoice_id
        }
        assert any(
            line.subscription_id is None and line.invoice_id == second.invoice_id
            for line in result.lines
        )
        assert any(
            s.id == census_rows.orphan_payment_id
            and s.kind == SourceKind.payment
            and s.account_id is None
            for s in result.financial_sources
        )
        assert any(
            e.source_id == census_rows.orphan_payment_id
            and e.code == ExceptionCode.account_mapping
            for e in result.blocking_exceptions
        )
        assert sum(c.exception_count for c in result.controls) == len(
            result.blocking_exceptions
        )
        control = next(
            c
            for c in result.controls
            if (c.account_id, c.currency) == (census_rows.subscriber_id, "NGN")
        )
        assert control.original_total == Decimal("200")
        assert control.canonical_discharge == Decimal("80")
        assert control.residual == Decimal("120")


def test_snapshot_stability_and_fresh_source_drift(
    census_factory: sessionmaker[Session], census_rows: SeededCensus
) -> None:
    requested = query(census_rows.cutoff)
    with app_db.read_only_snapshot_session() as db:
        first = preview_billing_opening_export(db, query=requested)
        with census_factory() as writer:
            draft = writer.get(Invoice, census_rows.draft_id)
            assert draft is not None
            draft.invoice_number = f"CHANGED-{uuid4().hex}"
            writer.commit()
        second = preview_billing_opening_export(db, query=requested)
        assert first.content_fingerprint == second.content_fingerprint
        assert first.capture.snapshot_id == second.capture.snapshot_id
    with app_db.read_only_snapshot_session() as db:
        fresh = preview_billing_opening_export(db, query=requested)
        assert fresh.content_fingerprint != first.content_fingerprint
        assert any(
            e.source_id == census_rows.draft_id
            and e.code == ExceptionCode.source_after_cutoff
            for e in fresh.blocking_exceptions
        )


def test_read_committed_and_pending_writes_are_rejected(
    census_factory: sessionmaker[Session], census_rows: SeededCensus
) -> None:
    with census_factory() as db:
        db.execute(text("SELECT 1"))
        with pytest.raises(BillingOpeningExportError) as error:
            preview_billing_opening_export(db, query=query(census_rows.cutoff))
        assert error.value.code.endswith("snapshot_required")
        assert db.in_transaction()
    with app_db.read_only_snapshot_session() as db:
        db.add(Invoice(account_id=census_rows.subscriber_id))
        with pytest.raises(BillingOpeningExportError) as error:
            preview_billing_opening_export(db, query=query(census_rows.cutoff))
        assert error.value.code.endswith("snapshot_required")
        assert db.new


def test_v2_raw_receipts_preserve_gross_net_orphans_and_same_snapshot(
    census_factory: sessionmaker[Session],
    census_rows: SeededCensus,
) -> None:
    from dataclasses import asdict, replace

    from app.models.billing import (
        PaymentProvider,
        PaymentProviderEvent,
        PaymentProviderEventSource,
        PaymentProviderEventStatus,
        PaymentSettlement,
        PaymentSettlementOrigin,
    )
    from app.models.integration_platform import (
        IntegrationCapabilityBinding,
        IntegrationInbox,
        IntegrationInstallation,
    )
    from app.services.billing.opening_export_contracts import (
        BillingOpeningExportPreviewV2,
    )
    from scripts.migration.billing_opening_export import _json_value

    with census_factory() as writer:
        provider = PaymentProvider(name=f"Census cash {uuid4().hex}")
        installation = IntegrationInstallation(
            connector_key="cash-census-test",
            connector_version="1",
            manifest_digest="d" * 64,
            name=f"Cash {uuid4().hex}",
            environment="test",
        )
        writer.add_all([provider, installation])
        writer.flush()
        binding = IntegrationCapabilityBinding(
            installation_id=installation.id, capability_id="payment.test"
        )
        payments = [
            Payment(
                account_id=census_rows.subscriber_id,
                provider_id=provider.id,
                amount=Decimal("100.00"),
                provider_fee=Decimal("2.00"),
                currency="NGN",
                status=PaymentStatus.succeeded,
            )
            for _ in range(2)
        ]
        writer.add_all([binding, *payments])
        writer.flush()
        receipts = [
            PaymentSettlement(
                payment_id=p.id,
                amount=Decimal("100.00") if i == 0 else Decimal("98.00"),
                unallocated_amount=Decimal("100.00") if i == 0 else Decimal("98.00"),
                currency="NGN",
                origin=PaymentSettlementOrigin.manual
                if i == 0
                else PaymentSettlementOrigin.provider_event,
            )
            for i, p in enumerate(payments)
        ]
        events = [
            PaymentProviderEvent(
                provider_id=provider.id,
                payment_id=payments[1].id if i < 2 else None,
                event_type="charge.success",
                external_id=f"cash-census-{uuid4().hex}",
                source=PaymentProviderEventSource.verified_webhook,
                status=PaymentProviderEventStatus.processed,
                amount=Decimal("100.00"),
                provider_fee=Decimal("2.00"),
                net_amount=Decimal("98.00"),
                currency="NGN",
                observation_digest="e" * 64,
                payload={"private-test-sentinel": "never-export"},
            )
            for i in range(3)
        ]
        writer.add_all([*receipts, *events])
        writer.flush()
        inbox = IntegrationInbox(
            installation_id=installation.id,
            capability_binding_id=binding.id,
            provider_event_id="cash-receipt",
            event_type="charge.success",
            payload_digest="f" * 64,
            state="processed",
            payload_json={"private-test-sentinel": "never-export"},
            headers_json={"private-test-sentinel": "never-export"},
            consequence_json={
                "provider_event_id": str(events[0].id),
                "payment_id": str(payments[1].id),
                "provider_id": str(provider.id),
                "private-test-sentinel": "never-export",
            },
        )
        writer.add(inbox)
        writer.flush()
        payment_ids = tuple(p.id for p in payments)
        event_ids = tuple(e.id for e in events)
        inbox_id = inbox.id
        writer.commit()
        cutoff = writer.execute(text("SELECT clock_timestamp()")).scalar_one()
    requested = replace(
        query(cutoff), population_version="sub-billing-opening-census-v2"
    )
    with app_db.read_only_snapshot_session() as db:
        first = preview_billing_opening_export(db, query=requested)
        assert isinstance(first, BillingOpeningExportPreviewV2)
        cash = first.cash_evidence
        stored = {s.payment_id: s for s in cash.payment_settlements}
        assert stored[payment_ids[0]].amount == Decimal("100.00")
        assert stored[payment_ids[1]].amount == Decimal("98.00")
        assert set(event_ids) <= {e.id for e in cash.provider_events}
        assert any(
            e.id == event_ids[2] and e.payment_id is None for e in cash.provider_events
        )
        assert any(
            r.id == inbox_id and r.consequence_provider_event_id == event_ids[0]
            for r in cash.inbox_receipts
        )
        assert any(
            e.source_id == event_ids[2] and e.code == ExceptionCode.settlement_mapping
            for e in cash.blocking_exceptions
        )
        assert any(
            e.source_id == payment_ids[1]
            and e.code == ExceptionCode.cash_event_ambiguity
            for e in cash.blocking_exceptions
        )
        assert cash.qualified_payment_ids == ()
        assert "private-test-sentinel" not in str(_json_value(asdict(first)))
        counts = {c.table.value: c.count for c in cash.source_counts}
        assert counts["payment_provider_events"] == len(cash.provider_events)
        with census_factory() as writer:
            changed = writer.get(PaymentProviderEvent, event_ids[0])
            assert changed is not None
            changed.processed_at = writer.execute(
                text("SELECT clock_timestamp()")
            ).scalar_one()
            writer.commit()
        second = preview_billing_opening_export(db, query=requested)
        assert first.content_fingerprint == second.content_fingerprint
    with app_db.read_only_snapshot_session() as db:
        fresh = preview_billing_opening_export(db, query=requested)
        assert isinstance(fresh, BillingOpeningExportPreviewV2)
        assert fresh.content_fingerprint != first.content_fingerprint
        assert any(
            e.source_id == event_ids[0] and e.code == ExceptionCode.source_after_cutoff
            for e in fresh.cash_evidence.blocking_exceptions
        )


def test_v3_observes_persisted_tax_without_live_defaults_or_original_qualification(
    census_factory: sessionmaker[Session], census_rows: SeededCensus
) -> None:
    import json
    from dataclasses import asdict, replace

    from sqlalchemy import select

    from app.models.billing import TaxRate
    from app.services.billing.opening_export_contracts import (
        BillingOpeningExportPreviewV3,
        OriginalEvidenceRequirement,
    )
    from scripts.migration.billing_opening_export import _json_value

    with census_factory() as writer:
        tax = TaxRate(name=f"Observed {uuid4().hex}", rate=Decimal("0.2000"))
        writer.add(tax)
        writer.flush()
        lines = tuple(
            writer.scalars(
                select(InvoiceLine)
                .where(InvoiceLine.invoice_id == census_rows.invoices[0])
                .order_by(InvoiceLine.id)
            )
        )
        snapshotted, absent = lines
        snapshotted.tax_rate_id = tax.id
        snapshotted.tax_rate_snapshot_version = 1
        snapshotted.tax_rate_code_snapshot = "HISTORICAL"
        snapshotted.tax_rate_percent_snapshot = Decimal("0.0750")
        snapshotted.tax_rate_is_active_snapshot = False
        snapshotted_id, absent_id, tax_id = snapshotted.id, absent.id, tax.id
        writer.commit()
        cutoff = writer.execute(text("SELECT clock_timestamp()")).scalar_one()
    requested = replace(
        query(cutoff), population_version="sub-billing-opening-census-v3"
    )
    with app_db.read_only_snapshot_session() as db:
        # A V3 call neither mutates nor adds fields to V1/V2 wire formats.
        older_queries = tuple(
            replace(requested, population_version=f"sub-billing-opening-census-v{v}")
            for v in (1, 2)
        )
        before = tuple(
            json.dumps(
                _json_value(asdict(preview_billing_opening_export(db, query=q))),
                sort_keys=True,
            )
            for q in older_queries
        )
        first = preview_billing_opening_export(db, query=requested)
        assert isinstance(first, BillingOpeningExportPreviewV3)
        observations = first.documentary_observations
        assert len(observations.current_invoices) == len(first.invoices)
        assert len(observations.current_lines) == len(first.lines)
        assert {g.invoice_id for g in observations.unavailable_original_evidence} == {
            i.invoice_id for i in first.invoices
        }
        assert census_rows.draft_id in {
            g.invoice_id for g in observations.unavailable_original_evidence
        }
        retained = {line.line_id: line for line in observations.current_lines}
        assert retained[snapshotted_id].tax_rate_percent_snapshot == Decimal("0.0750")
        assert retained[snapshotted_id].tax_rate_is_active_snapshot is False
        assert retained[absent_id].tax_rate_percent_snapshot is None
        assert retained[absent_id].tax_rate_snapshot_version is None
        assert all(
            OriginalEvidenceRequirement.documentary_digest in g.requirements
            for g in observations.unavailable_original_evidence
        )
        assert all(
            OriginalEvidenceRequirement.withholding_non_cash_replay_bindings
            in g.requirements
            for g in observations.unavailable_original_evidence
        )
        assert first.cash_evidence.qualified_payment_ids == ()
        after = tuple(
            json.dumps(
                _json_value(asdict(preview_billing_opening_export(db, query=q))),
                sort_keys=True,
            )
            for q in older_queries
        )
        # Capture clock is expected to advance; content bytes exclude it.
        for old, new in zip(before, after, strict=True):
            old_payload, new_payload = json.loads(old), json.loads(new)
            del old_payload["capture"]["captured_at"]
            del new_payload["capture"]["captured_at"]
            assert old_payload == new_payload
        with census_factory() as writer:
            changed_tax = writer.get(TaxRate, tax_id)
            assert changed_tax is not None
            changed_tax.rate = Decimal("0.9900")
            changed_line = writer.get(InvoiceLine, snapshotted_id)
            assert changed_line is not None
            changed_line.description = "Current documentary drift"
            writer.commit()
        second = preview_billing_opening_export(db, query=requested)
        assert first.content_fingerprint == second.content_fingerprint
        assert not db.new and not db.dirty and not db.deleted
    with app_db.read_only_snapshot_session() as db:
        fresh = preview_billing_opening_export(db, query=requested)
        assert isinstance(fresh, BillingOpeningExportPreviewV3)
        assert fresh.content_fingerprint != first.content_fingerprint
        line = next(
            line
            for line in fresh.documentary_observations.current_lines
            if line.line_id == snapshotted_id
        )
        assert line.description == "Current documentary drift"
        assert line.tax_rate_percent_snapshot == Decimal("0.0750")
        assert (
            fresh.documentary_observations.unavailable_original_evidence
            == observations.unavailable_original_evidence
        )
        assert any(
            e.source_id == snapshotted_id
            and e.code == ExceptionCode.source_after_cutoff
            for e in fresh.blocking_exceptions
        )


# Explicit expected census inventory; this is not the future global fence.
_LOCKED_CENSUS_TABLES = (
    "alembic_version",
    "subscribers",
    "invoices",
    "invoice_lines",
    "payments",
    "credit_notes",
    "ledger_entries",
    "customer_posting_groups",
    "customer_position_effects",
    "payment_refunds",
    "payment_reversals",
    "prepaid_funding_baselines",
    "customer_subledger_opening_positions",
    "payment_allocations",
    "credit_note_applications",
    "prepaid_opening_funding_consumptions",
)
_LOCKED_CASH_TABLES = (
    "payment_settlements",
    "payment_provider_events",
    "integration_inbox",
    "payment_proofs",
    "payment_proof_corrections",
    "topup_intents",
)


def _lock_census(db: Session, *, cash: bool = False, omit: str | None = None) -> None:
    names = _LOCKED_CENSUS_TABLES + (_LOCKED_CASH_TABLES if cash else ())
    # Only this test's fixed inventory can enter SQL identifier positions.
    db.execute(
        text(
            "LOCK TABLE "
            + ", ".join("public." + n for n in names if n != omit)
            + " IN SHARE ROW EXCLUSIVE MODE"
        )
    )


def test_locked_population_requires_actual_root_write_transaction_and_every_lock(
    census_factory: sessionmaker[Session],
    census_rows: SeededCensus,
) -> None:
    from app.services.billing.opening_export import (
        capture_locked_billing_opening_population,
    )
    from app.services.billing.opening_export_contracts import (
        CaptureLockedBillingOpeningPopulationQuery,
    )

    requested = CaptureLockedBillingOpeningPopulationQuery(query(census_rows.cutoff))
    with census_factory() as db:
        with pytest.raises(BillingOpeningExportError, match="root transaction"):
            capture_locked_billing_opening_population(db, query=requested)
    with census_factory.begin() as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        with pytest.raises(BillingOpeningExportError, match="Every census relation"):
            capture_locked_billing_opening_population(db, query=requested)
    with census_factory.begin() as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        _lock_census(db, omit="payment_allocations")
        # A row/read lock is insufficient even when every other table is fenced.
        db.execute(text("LOCK TABLE public.payment_allocations IN SHARE MODE"))
        with pytest.raises(BillingOpeningExportError, match="Every census relation"):
            capture_locked_billing_opening_population(db, query=requested)


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_locked_population_refuses_preexisting_repeatable_snapshot(
    census_factory: sessionmaker[Session],
    census_rows: SeededCensus,
    isolation: str,
) -> None:
    from app.services.billing.opening_export import (
        capture_locked_billing_opening_population,
    )
    from app.services.billing.opening_export_contracts import (
        CaptureLockedBillingOpeningPopulationQuery,
    )

    with census_factory.begin() as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL " + isolation))
        db.execute(text("SELECT count(*) FROM public.invoices"))
        _lock_census(db)
        with pytest.raises(
            BillingOpeningExportError, match="READ WRITE READ COMMITTED"
        ):
            capture_locked_billing_opening_population(
                db,
                query=CaptureLockedBillingOpeningPopulationQuery(
                    query(census_rows.cutoff)
                ),
            )


def test_locked_population_preserves_readonly_preview_and_has_no_classification(
    census_factory: sessionmaker[Session],
    census_rows: SeededCensus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.billing.opening_export import (
        capture_locked_billing_opening_population,
    )
    from app.services.billing.opening_export_contracts import (
        BillingOpeningExportPreviewV3,
        CaptureLockedBillingOpeningPopulationQuery,
    )

    requested = PreviewBillingOpeningExportQuery(
        "a" * 40,
        UUID(int=123),
        census_rows.cutoff,
        (("NGN", 2), ("USD", 2)),
        "sub-billing-opening-census-v3",
    )
    with app_db.read_only_snapshot_session() as db:
        preview = preview_billing_opening_export(db, query=requested)
        assert isinstance(preview, BillingOpeningExportPreviewV3)
        with pytest.raises(
            BillingOpeningExportError, match="READ WRITE READ COMMITTED"
        ):
            capture_locked_billing_opening_population(
                db, query=CaptureLockedBillingOpeningPopulationQuery(requested)
            )
    with census_factory.begin() as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        _lock_census(db, cash=True)
        original_autoflush = db.autoflush

        def forbidden(*args: object, **kwargs: object) -> None:
            pytest.fail("Locked census attempted mutation or classification")

        with monkeypatch.context() as guard:
            for name in (
                "add",
                "add_all",
                "delete",
                "flush",
                "commit",
                "rollback",
                "begin_nested",
            ):
                guard.setattr(db, name, forbidden)
            guard.setattr("app.services.billing.opening_export._classify", forbidden)
            guard.setattr(
                "app.services.billing.opening_export._classify_cash", forbidden
            )
            result = capture_locked_billing_opening_population(
                db, query=CaptureLockedBillingOpeningPopulationQuery(requested)
            )
        assert db.autoflush is original_autoflush and db.in_transaction()
        assert result.capture.isolation == "read committed"
        assert result.population.invoices == preview.invoices
        assert result.population.financial_sources == preview.financial_sources
        assert result.documentary_observations == preview.documentary_observations
        assert result.cash_evidence is not None and preview.cash_evidence is not None
        assert result.cash_evidence.payments == preview.cash_evidence.payments
        assert not result.cash_evidence.qualified_payment_ids
        assert not result.cash_evidence.blocking_exceptions
        with pytest.raises(BillingOpeningExportError, match="READ ONLY"):
            preview_billing_opening_export(db, query=requested)


def test_locked_population_sees_commit_while_waiting_for_table_fence(
    census_factory: sessionmaker[Session],
    census_rows: SeededCensus,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue
    from time import monotonic, sleep

    from app.services.billing.opening_export import (
        capture_locked_billing_opening_population,
    )
    from app.services.billing.opening_export_contracts import (
        CaptureLockedBillingOpeningPopulationQuery,
        LockedBillingOpeningPopulation,
    )

    pid_queue: Queue[int] = Queue()
    invoice_id = census_rows.invoices[0]
    marker = "committed-before-census-lock-" + uuid4().hex

    def capture() -> LockedBillingOpeningPopulation:
        with census_factory.begin() as db:
            db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            db.execute(text("SET LOCAL lock_timeout = '8s'"))
            # Deliberately read before lock acquisition: RC must refresh afterward.
            db.execute(
                text("SELECT invoice_number FROM invoices WHERE id = :id"),
                {"id": invoice_id},
            ).one()
            pid_queue.put(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
            _lock_census(db)
            return capture_locked_billing_opening_population(
                db,
                query=CaptureLockedBillingOpeningPopulationQuery(
                    query(census_rows.cutoff)
                ),
            )

    with ThreadPoolExecutor(max_workers=1) as pool, census_factory() as writer:
        writer.execute(
            text("UPDATE invoices SET invoice_number = :marker WHERE id = :id"),
            {"marker": marker, "id": invoice_id},
        )
        future = pool.submit(capture)
        pid = pid_queue.get(timeout=5)
        deadline = monotonic() + 5
        with census_factory() as observer:
            while True:
                waiting = observer.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = :pid "
                        "AND relation = 'public.invoices'::regclass AND NOT granted "
                        "AND mode = 'ShareRowExclusiveLock')"
                    ),
                    {"pid": pid},
                ).scalar_one()
                if waiting:
                    break
                assert monotonic() < deadline, (
                    "Capture never waited on the active source writer"
                )
                sleep(0.02)
        writer.commit()
        result = future.result(timeout=10)
    assert (
        next(
            i for i in result.population.invoices if i.invoice_id == invoice_id
        ).invoice_number
        == marker
    )


def test_locked_population_refuses_dirty_and_nested_callers(
    census_factory: sessionmaker[Session],
    census_rows: SeededCensus,
) -> None:
    from app.services.billing.opening_export import (
        capture_locked_billing_opening_population,
    )
    from app.services.billing.opening_export_contracts import (
        CaptureLockedBillingOpeningPopulationQuery,
    )

    requested = CaptureLockedBillingOpeningPopulationQuery(query(census_rows.cutoff))
    with census_factory() as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        _lock_census(db)
        invoice = db.get(Invoice, census_rows.invoices[0])
        assert invoice is not None
        invoice.invoice_number = "pending-never-flushed-" + uuid4().hex
        with pytest.raises(BillingOpeningExportError, match="root transaction"):
            capture_locked_billing_opening_population(db, query=requested)
        assert invoice in db.dirty
    with census_factory.begin() as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        _lock_census(db)
        with db.begin_nested():
            with pytest.raises(BillingOpeningExportError, match="root transaction"):
                capture_locked_billing_opening_population(db, query=requested)


def test_locked_population_refuses_inherited_relation_even_with_parent_lock(
    census_factory: sessionmaker[Session],
    census_rows: SeededCensus,
) -> None:
    from app.services.billing.opening_export import (
        capture_locked_billing_opening_population,
    )
    from app.services.billing.opening_export_contracts import (
        CaptureLockedBillingOpeningPopulationQuery,
    )

    # Roll back catalog perturbation; migrations still own all actual source tables.
    with census_factory() as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        child = "census_inheritance_canary_" + uuid4().hex
        db.execute(
            text("CREATE TABLE public." + child + " () INHERITS (public.invoices)")
        )
        _lock_census(db)
        with pytest.raises(BillingOpeningExportError, match="Every census relation"):
            capture_locked_billing_opening_population(
                db,
                query=CaptureLockedBillingOpeningPopulationQuery(
                    query(census_rows.cutoff)
                ),
            )


def test_locked_population_refuses_search_path_shadow_even_with_public_locks(
    census_factory: sessionmaker[Session],
    census_rows: SeededCensus,
) -> None:
    from app.services.billing.opening_export import (
        capture_locked_billing_opening_population,
    )
    from app.services.billing.opening_export_contracts import (
        CaptureLockedBillingOpeningPopulationQuery,
    )

    with census_factory() as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        _lock_census(db)
        db.execute(text("CREATE TEMPORARY TABLE invoices (id uuid) ON COMMIT DROP"))
        with pytest.raises(BillingOpeningExportError, match="Every census relation"):
            capture_locked_billing_opening_population(
                db,
                query=CaptureLockedBillingOpeningPopulationQuery(
                    query(census_rows.cutoff)
                ),
            )


@pytest.mark.parametrize("translation_target", ["bind", "connection"])
def test_locked_population_refuses_schema_translation_before_population_read(
    census_factory: sessionmaker[Session],
    census_rows: SeededCensus,
    monkeypatch: pytest.MonkeyPatch,
    translation_target: str,
) -> None:
    from app.services.billing.opening_export import (
        capture_locked_billing_opening_population,
    )
    from app.services.billing.opening_export_contracts import (
        CaptureLockedBillingOpeningPopulationQuery,
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Translated session reached the population read")

    monkeypatch.setattr("app.services.billing.opening_export._population", forbidden)
    with census_factory() as source:
        engine = source.get_bind()
        assert isinstance(engine, Engine)
        if translation_target == "bind":
            engine = engine.execution_options(schema_translate_map={None: "pg_temp"})
        with Session(bind=engine) as db:
            db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            _lock_census(db)
            if translation_target == "connection":
                db.connection().execution_options(
                    schema_translate_map={None: "pg_temp"}
                )
            with pytest.raises(BillingOpeningExportError, match="untranslated"):
                capture_locked_billing_opening_population(
                    db,
                    query=CaptureLockedBillingOpeningPopulationQuery(
                        query(census_rows.cutoff)
                    ),
                )


@pytest.mark.parametrize("binding_target", ["mapper", "table"])
@pytest.mark.parametrize(
    "model_name",
    [
        "Invoice",
        "Payment",
        "PaymentAllocation",
        "CreditNoteApplication",
        "PrepaidOpeningFundingConsumption",
        "CustomerPostingGroup",
        "PaymentSettlement",
    ],
)
def test_locked_population_refuses_alternate_binding_before_population_read(
    census_factory: sessionmaker[Session],
    census_rows: SeededCensus,
    monkeypatch: pytest.MonkeyPatch,
    binding_target: str,
    model_name: str,
) -> None:
    from app.services.billing import opening_export
    from app.services.billing.opening_export_contracts import (
        CaptureLockedBillingOpeningPopulationQuery,
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Multi-bind session reached the population read")

    monkeypatch.setattr(opening_export, "_population", forbidden)
    models = (
        opening_export._BASE_POPULATION_MODELS + opening_export._CASH_POPULATION_MODELS
    )
    model = next(model for model in models if model.__name__ == model_name)
    with census_factory() as source:
        engine = source.get_bind()
        assert isinstance(engine, Engine)
    with census_factory() as db, engine.connect() as alternate:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        _lock_census(db, cash=True)
        if binding_target == "mapper":
            db.bind_mapper(model, alternate)
        else:
            db.bind_table(model.__table__, alternate)
        with pytest.raises(BillingOpeningExportError, match="lock-check bind"):
            opening_export.capture_locked_billing_opening_population(
                db,
                query=CaptureLockedBillingOpeningPopulationQuery(
                    replace(
                        query(census_rows.cutoff),
                        population_version="sub-billing-opening-census-v3",
                    )
                ),
            )
        assert not alternate.in_transaction()


@pytest.mark.parametrize("external_savepoint", [False, True])
def test_locked_population_checks_external_connection_root_transaction(
    census_factory: sessionmaker[Session],
    census_rows: SeededCensus,
    monkeypatch: pytest.MonkeyPatch,
    external_savepoint: bool,
) -> None:
    from app.services.billing import opening_export
    from app.services.billing.opening_export_contracts import (
        CaptureLockedBillingOpeningPopulationQuery,
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("External savepoint reached the population read")

    with census_factory() as source:
        engine = source.get_bind()
        assert isinstance(engine, Engine)
    with engine.connect() as connection, connection.begin():
        connection.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        savepoint = connection.begin_nested() if external_savepoint else None
        try:
            with Session(bind=connection) as db:
                _lock_census(db)
                assert not db.in_nested_transaction()
                assert connection.in_nested_transaction() is external_savepoint
                requested = CaptureLockedBillingOpeningPopulationQuery(
                    query(census_rows.cutoff)
                )
                if external_savepoint:
                    monkeypatch.setattr(opening_export, "_population", forbidden)
                    with pytest.raises(
                        BillingOpeningExportError, match="root connection"
                    ):
                        opening_export.capture_locked_billing_opening_population(
                            db, query=requested
                        )
                else:
                    result = opening_export.capture_locked_billing_opening_population(
                        db, query=requested
                    )
                    assert result.capture.isolation == "read committed"
                    assert result.population.invoices
                    assert connection.in_transaction()
        finally:
            if savepoint is not None and savepoint.is_active:
                savepoint.rollback()
