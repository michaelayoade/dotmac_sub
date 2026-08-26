"""PostgreSQL structural guarantees for the receivable projection.

The fast SQLite lane proves the reconciler *converges*. It cannot prove the
things that make the monotonic guard structural rather than conventional,
because those are migration-owned PostgreSQL objects:

* `billing_receivable_projection_version_seq`, which makes the version monotonic across
  concurrent workers rather than across one process's memory;
* `trg_billing_receivable_projections_monotonic`, which refuses any update that does
  not strictly advance `projection_version` or that moves `source_observed_at`
  backwards — the layer that catches a future writer who forgets the upsert
  predicate;
* the `ON CONFLICT ... WHERE excluded.source_observed_at > source_observed_at`
  predicate, which closes the window between the planner's read and its write.

These tests bypass the service deliberately in places. That is the point: they
assert the DATABASE refuses the write, not that the service declines to attempt
it. A guard that only holds while its one caller behaves is a convention.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import delete, inspect, select, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session, sessionmaker

from app.models.billing import Invoice, InvoiceDueDateBasis, InvoiceLine, InvoiceStatus
from app.models.billing_receivable_projection import BillingReceivableProjection
from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    OfferVersion,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Reseller, Subscriber
from app.services.billing.receivable_cohort import ReceivableCohortWindow
from app.services.billing.receivable_projection import (
    ProjectionMode,
    ReconcileReceivableProjectionCommand,
    reconcile_receivable_projection,
)
from app.services.owner_commands import CommandContext

WINDOW_START = datetime(2026, 7, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 1, tzinfo=UTC)
CUTOFF = datetime(2026, 8, 2, tzinfo=UTC)
ISSUED = datetime(2026, 7, 10, tzinfo=UTC)


def _window() -> ReceivableCohortWindow:
    return ReceivableCohortWindow(
        cutoff_at=CUTOFF, window_start=WINDOW_START, window_end=WINDOW_END
    )


def _command(**overrides) -> ReconcileReceivableProjectionCommand:
    base = {
        "context": CommandContext.system(
            actor="pytest:receivable-projection-pg",
            scope="receivable-projection",
            reason="PostgreSQL structural guard coverage",
            idempotency_key=f"pytest-pg:{uuid.uuid4()}",
        ),
        "window": _window(),
        "code_version": "pytest-pg",
        "database_schema_version": "558_receivable_observation_projection",
        "mode": ProjectionMode.APPLY,
    }
    base.update(overrides)
    return ReconcileReceivableProjectionCommand(**base)


@pytest.fixture()
def seeded_invoice(db_session, subscriber, subscription):
    subscription.billing_mode = BillingMode.postpaid
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal("25000.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("25000.00"),
        balance_due=Decimal("25000.00"),
        billing_period_start=WINDOW_START,
        billing_period_end=WINDOW_END,
        issued_at=ISSUED,
        due_at=ISSUED + timedelta(days=14),
        due_date_basis=InvoiceDueDateBasis.contract_terms,
        due_date_basis_ref=f"subscription:{subscription.id}",
        due_date_policy_version="billing-payment-terms-v1",
        created_at=datetime(2026, 7, 5, tzinfo=UTC),
        updated_at=datetime(2026, 7, 5, tzinfo=UTC),
        is_active=True,
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Standard Internet",
            quantity=Decimal("1.000"),
            unit_price=Decimal("25000.00"),
            amount=Decimal("25000.00"),
            is_active=True,
            created_at=datetime(2026, 7, 5, tzinfo=UTC),
            updated_at=datetime(2026, 7, 5, tzinfo=UTC),
        )
    )
    db_session.commit()
    return invoice


def _seed_committed_invoice(
    session_factory: sessionmaker[Session],
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create worker-visible source rows outside the fixture savepoint.

    The shared ``db_session`` fixture intentionally holds data inside an outer
    transaction. Independent concurrency workers cannot see those rows, so a
    concurrency proof must own and commit its own uniquely named source data.
    """
    suffix = uuid.uuid4().hex[:12]
    with session_factory() as setup:
        reseller = Reseller(
            name=f"Receivable Projection {suffix}",
            code=f"receivable-projection-{suffix}",
            is_active=True,
        )
        account = Subscriber(
            first_name="Receivable",
            last_name="Projection",
            email=f"receivable-projection-{suffix}@example.test",
            reseller=reseller,
            billing_mode=BillingMode.postpaid,
        )
        offer = CatalogOffer(
            name=f"Receivable Projection {suffix}",
            code=f"RP-{suffix}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            billing_cycle=BillingCycle.monthly,
            billing_mode=BillingMode.postpaid,
        )
        offer_version = OfferVersion(
            offer=offer,
            version_number=1,
            name=f"Receivable Projection {suffix} v1",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            billing_cycle=BillingCycle.monthly,
        )
        subscription = Subscription(
            subscriber=account,
            offer=offer,
            offer_version=offer_version,
            status=SubscriptionStatus.active,
            billing_mode=BillingMode.postpaid,
            billing_cycle=BillingCycle.monthly,
            start_at=WINDOW_START,
            next_billing_at=WINDOW_END,
        )
        setup.add_all([reseller, account, offer, offer_version, subscription])
        setup.flush()
        invoice = Invoice(
            account_id=account.id,
            status=InvoiceStatus.issued,
            currency="NGN",
            subtotal=Decimal("25000.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("25000.00"),
            balance_due=Decimal("25000.00"),
            billing_period_start=WINDOW_START,
            billing_period_end=WINDOW_END,
            issued_at=ISSUED,
            due_at=ISSUED + timedelta(days=14),
            due_date_basis=InvoiceDueDateBasis.contract_terms,
            due_date_basis_ref=f"subscription:{subscription.id}",
            due_date_policy_version="billing-payment-terms-v1",
            created_at=datetime(2026, 7, 5, tzinfo=UTC),
            updated_at=datetime(2026, 7, 5, tzinfo=UTC),
            is_active=True,
        )
        setup.add(invoice)
        setup.flush()
        setup.add(
            InvoiceLine(
                invoice_id=invoice.id,
                subscription_id=subscription.id,
                description="Standard Internet",
                quantity=Decimal("1.000"),
                unit_price=Decimal("25000.00"),
                amount=Decimal("25000.00"),
                is_active=True,
                created_at=datetime(2026, 7, 5, tzinfo=UTC),
                updated_at=datetime(2026, 7, 5, tzinfo=UTC),
            )
        )
        setup.commit()
        return invoice.id, subscription.id


# ── The migration actually installed the structural objects ─────────────────


def test_the_sequence_and_trigger_exist_in_the_migrated_schema(engine):
    with engine.connect() as connection:
        sequence = connection.execute(
            text(
                "SELECT 1 FROM pg_class WHERE relkind = 'S' "
                "AND relname = 'billing_receivable_projection_version_seq'"
            )
        ).scalar_one_or_none()
        trigger = connection.execute(
            text(
                "SELECT 1 FROM pg_trigger "
                "WHERE tgname = 'trg_billing_receivable_projections_monotonic' "
                "AND NOT tgisinternal"
            )
        ).scalar_one_or_none()
    assert sequence == 1, "migration 558 must create the projection version sequence"
    assert trigger == 1, "migration 558 must install the monotonic trigger"


def test_the_projection_is_not_tenant_scoped_and_has_no_rls(engine):
    """A deliberate absence, asserted so a half-added column cannot survive.

    Every authoritative input is tenant-free; Sub's tenancy is the ADR-0009
    operator bridge, not a column on financial tables. A `tenant_id` here would
    have nothing authoritative to fill it and the RLS policy over it would be
    decorative rather than isolating.
    """
    columns = {
        column["name"]
        for column in inspect(engine).get_columns("billing_receivable_projections")
    }
    assert "tenant_id" not in columns

    with engine.connect() as connection:
        forced = connection.execute(
            text(
                "SELECT relrowsecurity FROM pg_class "
                "WHERE relname = 'billing_receivable_projections'"
            )
        ).scalar_one()
        policies = connection.execute(
            text(
                "SELECT count(*) FROM pg_policies "
                "WHERE tablename = 'billing_receivable_projections'"
            )
        ).scalar_one()
    assert forced is False
    assert policies == 0


# ── The trigger refuses what the predicate is only supposed to avoid ────────


def test_the_database_refuses_a_non_advancing_projection_version(
    db_session, seeded_invoice
):
    """Bypass the service on purpose: the DATABASE must be the one that refuses."""
    reconcile_receivable_projection(db_session, _command())
    db_session.commit()
    row = db_session.execute(select(BillingReceivableProjection)).scalars().one()

    with pytest.raises(DatabaseError) as excinfo:
        db_session.execute(
            text(
                "UPDATE billing_receivable_projections SET projection_version = :value "
                "WHERE id = :id"
            ),
            {"value": row.projection_version, "id": row.id},
        )
    db_session.rollback()
    assert "must strictly" in str(excinfo.value)


def test_the_database_refuses_a_backwards_source_watermark(db_session, seeded_invoice):
    reconcile_receivable_projection(db_session, _command())
    db_session.commit()
    row = db_session.execute(select(BillingReceivableProjection)).scalars().one()

    with pytest.raises(DatabaseError) as excinfo:
        db_session.execute(
            text(
                "UPDATE billing_receivable_projections "
                "SET projection_version = projection_version + 1, "
                "    source_observed_at = :older "
                "WHERE id = :id"
            ),
            {"older": row.source_observed_at - timedelta(days=1), "id": row.id},
        )
    db_session.rollback()
    assert "must not move" in str(excinfo.value)


def test_the_trigger_guard_still_bites(db_session, seeded_invoice):
    """Sensitivity proof: a legitimate advance must still be accepted.

    Without this, a trigger that rejected every update would make both
    rejection tests pass while breaking the reconciler entirely.
    """
    reconcile_receivable_projection(db_session, _command())
    db_session.commit()
    row = db_session.execute(select(BillingReceivableProjection)).scalars().one()
    previous_version = row.projection_version

    db_session.execute(
        text(
            "UPDATE billing_receivable_projections "
            "SET projection_version = projection_version + 1, "
            "    source_observed_at = :newer "
            "WHERE id = :id"
        ),
        {"newer": row.source_observed_at + timedelta(days=1), "id": row.id},
    )
    db_session.commit()
    refreshed = db_session.execute(select(BillingReceivableProjection)).scalars().one()
    assert refreshed.projection_version == previous_version + 1


# ── Concurrency ─────────────────────────────────────────────────────────────


def test_concurrent_passes_project_one_row_and_advance_the_version_once(
    engine,
):
    """Two workers, one natural key.

    The advisory lock serialises the passes; the upsert predicate makes the
    loser a no-op rather than a regression; the sequence guarantees the winner's
    version is greater than anything already stored.
    """
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    invoice_id, subscription_id = _seed_committed_invoice(session_factory)
    barrier = Barrier(2)

    def run() -> int:
        with session_factory() as worker:
            barrier.wait(timeout=15)
            result = reconcile_receivable_projection(worker, _command())
            return result.inserted_count

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            inserted = [
                future.result(timeout=60)
                for future in [pool.submit(run), pool.submit(run)]
            ]

        with session_factory() as check:
            rows = (
                check.execute(
                    select(BillingReceivableProjection).where(
                        BillingReceivableProjection.invoice_id == invoice_id
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1, "the natural key must arbitrate concurrent passes"
        assert sum(inserted) == 1, "exactly one pass may claim the insert"
    finally:
        # These rows were committed outside the fixture transaction so workers
        # could observe them. Remove the cohort inputs before later tests run.
        with session_factory.begin() as cleanup:
            cleanup.execute(
                delete(BillingReceivableProjection).where(
                    BillingReceivableProjection.invoice_id == invoice_id
                )
            )
            cleanup.execute(
                delete(InvoiceLine).where(InvoiceLine.invoice_id == invoice_id)
            )
            cleanup.execute(delete(Invoice).where(Invoice.id == invoice_id))
            cleanup.execute(
                delete(Subscription).where(Subscription.id == subscription_id)
            )


def test_the_sequence_is_monotonic_across_sessions(engine):
    session_factory = sessionmaker(bind=engine)
    with session_factory() as first:
        low = first.execute(
            text("SELECT nextval('billing_receivable_projection_version_seq')")
        ).scalar_one()
        first.commit()
    with session_factory() as second:
        high = second.execute(
            text("SELECT nextval('billing_receivable_projection_version_seq')")
        ).scalar_one()
        second.commit()
    assert high > low


# ── Repair converges on the migrated schema ─────────────────────────────────


def test_a_repair_pass_converges_and_writes_no_duplicate(db_session, seeded_invoice):
    reconcile_receivable_projection(db_session, _command())
    db_session.commit()
    first = db_session.execute(select(BillingReceivableProjection)).scalars().one()
    first_version = first.projection_version

    seeded_invoice.balance_due = Decimal("5000.00")
    seeded_invoice.status = InvoiceStatus.partially_paid
    seeded_invoice.updated_at = datetime(2026, 7, 25, tzinfo=UTC)
    db_session.commit()

    result = reconcile_receivable_projection(db_session, _command())
    db_session.commit()
    rows = db_session.execute(select(BillingReceivableProjection)).scalars().all()

    assert result.updated_count == 1
    assert len(rows) == 1
    assert rows[0].projection_version > first_version
    assert rows[0].observed_outstanding_amount == Decimal("5000.0000")


def test_the_subscription_fixture_is_actually_postpaid(db_session, subscription):
    """Sensitivity proof for the lane assertions in the fast lane."""
    live = db_session.get(Subscription, subscription.id)
    assert live is not None
    assert live.billing_mode is BillingMode.postpaid
