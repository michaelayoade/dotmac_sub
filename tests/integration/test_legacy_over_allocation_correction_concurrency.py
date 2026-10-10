"""PostgreSQL serialization for the legacy over-allocation return.

Confirmation locks account, invoice, payment, and every allocation of the
invoice before recomputing its fingerprint, so concurrent confirmations
converge: with one idempotency key the second replays the stored outcome; with
two keys the second sees the allocation already returned and is refused as
stale. Exactly one return and one audit row exist afterwards, the invoice stays
paid, and no ledger entry is added.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from threading import Barrier

from sqlalchemy.orm import sessionmaker

from app.models.audit import AuditEvent
from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentStatus,
)
from app.models.catalog import BillingMode
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.models.system_user import SystemUser
from app.services.billing.legacy_over_allocation_correction import (
    CORRECTION_PERMISSION,
    LegacyOverAllocationError,
    LegacyOverAllocationQuery,
    ReturnLegacyOverAllocationCommand,
    preview_legacy_over_allocation_return,
    return_legacy_over_allocation,
)
from app.services.owner_commands import CommandContext

TOTAL = Decimal("17500.00")
EXCESS = Decimal("18812.50")
PAID_AT = datetime(2026, 6, 16, 16, 53, tzinfo=UTC)


@dataclass(frozen=True)
class _Fixture:
    session_factory: sessionmaker
    account_id: uuid.UUID
    invoice_id: uuid.UUID
    excess_allocation_id: uuid.UUID
    keeper_allocation_id: uuid.UUID
    query: LegacyOverAllocationQuery
    staff_id: uuid.UUID


def _context(user_id: uuid.UUID, key: str) -> CommandContext:
    return CommandContext.system(
        actor=f"user:{user_id}",
        scope=CORRECTION_PERMISSION,
        reason="PostgreSQL legacy over-allocation return concurrency",
        idempotency_key=key,
    )


def _setup(engine) -> _Fixture:
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    with session_factory() as setup:
        reseller = Reseller(
            name=f"Legacy Alloc {suffix}", code=f"legacy-alloc-{suffix}", is_active=True
        )
        account = Subscriber(
            first_name="Legacy",
            last_name="Allocation",
            email=f"legacy-alloc-{suffix}@example.com",
            reseller=reseller,
            status=SubscriberStatus.active,
            is_active=True,
            billing_enabled=True,
            billing_mode=BillingMode.prepaid,
        )
        staff = SystemUser(
            first_name="Finance",
            last_name="Operator",
            display_name="Finance Operator",
            email=f"finance-{suffix}@example.test",
            is_active=True,
        )
        setup.add_all([reseller, account, staff])
        setup.flush()
        invoice = Invoice(
            account_id=account.id,
            invoice_number=f"INV-PG-LEGACY-{suffix}",
            status=InvoiceStatus.paid,
            currency="NGN",
            subtotal=TOTAL,
            total=TOTAL,
            balance_due=Decimal("0.00"),
            issued_at=PAID_AT,
            paid_at=PAID_AT,
        )
        keeper_payment = Payment(
            splynx_payment_id=int(uuid4().int % 10**9),
            account_id=account.id,
            amount=TOTAL,
            currency="NGN",
            status=PaymentStatus.succeeded,
            paid_at=PAID_AT,
        )
        excess_payment = Payment(
            splynx_payment_id=int(uuid4().int % 10**9),
            account_id=account.id,
            amount=EXCESS,
            currency="NGN",
            status=PaymentStatus.succeeded,
            paid_at=PAID_AT,
        )
        setup.add_all([invoice, keeper_payment, excess_payment])
        setup.flush()
        setup.add(
            InvoiceLine(
                invoice_id=invoice.id,
                description="Unlimited Basic",
                quantity=Decimal("1.000"),
                unit_price=TOTAL,
                amount=TOTAL,
            )
        )
        keeper = PaymentAllocation(
            payment_id=keeper_payment.id, invoice_id=invoice.id, amount=TOTAL
        )
        excess = PaymentAllocation(
            payment_id=excess_payment.id, invoice_id=invoice.id, amount=EXCESS
        )
        setup.add_all(
            [
                keeper,
                excess,
                LedgerEntry(
                    account_id=account.id,
                    invoice_id=invoice.id,
                    payment_id=keeper_payment.id,
                    entry_type=LedgerEntryType.credit,
                    source=LedgerSource.payment,
                    amount=TOTAL,
                    currency="NGN",
                ),
                LedgerEntry(
                    account_id=account.id,
                    payment_id=excess_payment.id,
                    entry_type=LedgerEntryType.credit,
                    source=LedgerSource.payment,
                    amount=EXCESS,
                    currency="NGN",
                ),
            ]
        )
        setup.commit()
        return _Fixture(
            session_factory=session_factory,
            account_id=account.id,
            invoice_id=invoice.id,
            excess_allocation_id=excess.id,
            keeper_allocation_id=keeper.id,
            query=LegacyOverAllocationQuery(
                allocation_id=excess.id,
                expected_amount=EXCESS,
                expected_invoice_total=TOTAL,
                expected_remaining_settlement=TOTAL,
            ),
            staff_id=staff.id,
        )


def _fingerprint(fixture: _Fixture) -> str:
    with fixture.session_factory() as db:
        preview = preview_legacy_over_allocation_return(db, fixture.query)
        db.commit()
        assert preview.actionable, preview.blockers
        return preview.fingerprint


def _confirm(fixture: _Fixture, *, fingerprint: str, key: str, barrier: Barrier) -> str:
    with fixture.session_factory() as db:
        barrier.wait(timeout=10)
        try:
            result = return_legacy_over_allocation(
                db,
                ReturnLegacyOverAllocationCommand(
                    query=fixture.query,
                    preview_fingerprint=fingerprint,
                    reason="Finance decided the over-allocation is account credit",
                    evidence_reference="FIN-PG-3",
                    evidence_sha256="e" * 64,
                    reviewed_by=fixture.staff_id,
                    permission_granted=True,
                ),
                context=_context(fixture.staff_id, key),
            )
        except LegacyOverAllocationError as exc:
            return exc.code.rsplit(".", maxsplit=1)[-1]
        return "replayed" if result.replayed else "applied"


def _assert_one_return(fixture: _Fixture) -> None:
    with fixture.session_factory() as check:
        excess = check.get(PaymentAllocation, fixture.excess_allocation_id)
        keeper = check.get(PaymentAllocation, fixture.keeper_allocation_id)
        invoice = check.get(Invoice, fixture.invoice_id)
        assert excess is not None and keeper is not None and invoice is not None
        assert excess.is_active is False
        assert excess.reversed_at is not None
        assert keeper.is_active is True
        assert invoice.status is InvoiceStatus.paid
        assert invoice.balance_due == Decimal("0.00")
        assert (
            check.query(LedgerEntry).filter_by(account_id=fixture.account_id).count()
            == 2
        )
        assert (
            check.query(AuditEvent)
            .filter_by(
                action="return_legacy_over_allocation_to_account_credit",
                entity_id=str(fixture.excess_allocation_id),
            )
            .count()
            == 1
        )


def test_concurrent_confirmations_with_one_key_converge(engine) -> None:
    fixture = _setup(engine)
    fingerprint = _fingerprint(fixture)
    key = f"pg-legacy-{uuid.uuid4().hex}"
    barrier = Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda _index: _confirm(
                    fixture, fingerprint=fingerprint, key=key, barrier=barrier
                ),
                range(2),
            )
        )

    assert sorted(outcomes) == ["applied", "replayed"]
    _assert_one_return(fixture)


def test_concurrent_confirmations_with_two_keys_apply_once(engine) -> None:
    fixture = _setup(engine)
    fingerprint = _fingerprint(fixture)
    barrier = Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda index: _confirm(
                    fixture,
                    fingerprint=fingerprint,
                    key=f"pg-legacy-{index}-{uuid.uuid4().hex}",
                    barrier=barrier,
                ),
                range(2),
            )
        )

    assert sorted(outcomes) == ["applied", "stale_preview"]
    _assert_one_return(fixture)
