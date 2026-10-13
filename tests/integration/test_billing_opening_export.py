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
