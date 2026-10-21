"""Hosted-only census acceptance against real Alembic PostgreSQL schema."""

from dataclasses import dataclass
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
