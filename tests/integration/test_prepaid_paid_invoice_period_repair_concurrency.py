"""PostgreSQL serialization for the finance-reviewed paid-invoice period repair.

The approval locks account, subscription, invoice, line, and entitlements
before recomputing its fingerprint, so concurrent approvals converge: one
applies, the other either replays the stored outcome (same request) or sees
stale evidence (a competing request for the same invoice). Exactly one
entitlement and one period restoration exist afterwards.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier

from sqlalchemy.orm import sessionmaker

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
    ServiceEntitlement,
)
from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.models.system_user import SystemUser
from app.services.owner_commands import CommandContext
from app.services.prepaid_paid_invoice_period_repair import (
    REPAIR_PERMISSION,
    ApprovePaidInvoicePeriodRepairCommand,
    PaidInvoicePeriodRepairError,
    PaidInvoicePeriodRepairQuery,
    RequestPaidInvoicePeriodRepairCommand,
    approve_paid_invoice_period_repair,
    preview_paid_invoice_period_repair,
    request_paid_invoice_period_repair,
)

PRICE = Decimal("17500.00")


@dataclass(frozen=True)
class _Fixture:
    session_factory: sessionmaker
    invoice_id: uuid.UUID
    subscription_id: uuid.UUID
    query: PaidInvoicePeriodRepairQuery
    requester_id: uuid.UUID
    approver_ids: tuple[uuid.UUID, uuid.UUID]


def _context(user_id: uuid.UUID, key: str) -> CommandContext:
    return CommandContext.system(
        actor=f"user:{user_id}",
        scope=REPAIR_PERMISSION,
        reason="PostgreSQL paid invoice period repair concurrency",
        idempotency_key=key,
    )


def _setup(engine) -> _Fixture:
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    start = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
    end = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    with session_factory() as setup:
        reseller = Reseller(
            name=f"Period Repair {suffix}",
            code=f"period-repair-{suffix}",
            is_active=True,
        )
        account = Subscriber(
            first_name="Period",
            last_name="Repair",
            email=f"period-repair-{suffix}@example.com",
            reseller=reseller,
            status=SubscriberStatus.active,
            is_active=True,
            billing_enabled=True,
            billing_mode=BillingMode.prepaid,
        )
        offer = CatalogOffer(
            name=f"Period Repair Offer {suffix}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            status=OfferStatus.active,
            is_active=True,
            billing_mode=BillingMode.prepaid,
        )
        staff = [
            SystemUser(
                first_name=name,
                last_name="Finance",
                display_name=f"{name} Finance",
                email=f"{name.lower()}-{suffix}@example.test",
                is_active=True,
            )
            for name in ("Requester", "ApproverOne", "ApproverTwo")
        ]
        setup.add_all([reseller, account, offer, *staff])
        setup.flush()
        subscription = Subscription(
            subscriber_id=account.id,
            offer_id=offer.id,
            status=SubscriptionStatus.active,
            billing_mode=BillingMode.prepaid,
            billing_cycle=BillingCycle.monthly,
            unit_price=PRICE,
            start_at=start - timedelta(days=30),
            next_billing_at=datetime.now(UTC) - timedelta(days=5),
        )
        invoice = Invoice(
            account_id=account.id,
            invoice_number=f"INV-PG-PERIOD-{suffix}",
            status=InvoiceStatus.paid,
            currency="NGN",
            subtotal=PRICE,
            total=PRICE,
            balance_due=Decimal("0.00"),
            issued_at=start,
            paid_at=start,
        )
        payment = Payment(
            account_id=account.id,
            amount=PRICE,
            currency="NGN",
            status=PaymentStatus.succeeded,
        )
        setup.add_all([subscription, invoice, payment])
        setup.flush()
        line = InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Monthly service",
            quantity=Decimal("1.000"),
            unit_price=PRICE,
            amount=PRICE,
            metadata_={"kind": "base_subscription"},
        )
        ledger = LedgerEntry(
            account_id=account.id,
            invoice_id=invoice.id,
            payment_id=payment.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=PRICE,
            currency="NGN",
        )
        settlement = PaymentSettlement(
            payment_id=payment.id,
            amount=PRICE,
            unallocated_amount=Decimal("0.00"),
            currency="NGN",
            origin=PaymentSettlementOrigin.system,
        )
        setup.add_all([line, ledger, settlement])
        setup.flush()
        setup.add(
            PaymentAllocation(
                payment_id=payment.id,
                invoice_id=invoice.id,
                ledger_entry_id=ledger.id,
                amount=PRICE,
            )
        )
        setup.commit()
        query = PaidInvoicePeriodRepairQuery(
            invoice_id=invoice.id,
            line_id=line.id,
            subscription_id=subscription.id,
            period_start=start,
            period_end=end,
        )
        return _Fixture(
            session_factory=session_factory,
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            query=query,
            requester_id=staff[0].id,
            approver_ids=(staff[1].id, staff[2].id),
        )


def _request(fixture: _Fixture, key: str) -> tuple[uuid.UUID, str]:
    with fixture.session_factory() as db:
        fingerprint = preview_paid_invoice_period_repair(db, fixture.query).fingerprint
        db.commit()
        result = request_paid_invoice_period_repair(
            db,
            RequestPaidInvoicePeriodRepairCommand(
                query=fixture.query,
                preview_fingerprint=fingerprint,
                reason="Splynx invoice and receipt prove the April 2026 cycle",
                evidence_reference="FIN-PG-1",
                evidence_sha256="c" * 64,
                requested_by=fixture.requester_id,
                permission_granted=True,
            ),
            context=_context(fixture.requester_id, key),
        )
        return result.request_id, fingerprint


def _approve(
    fixture: _Fixture,
    *,
    request_id: uuid.UUID,
    fingerprint: str,
    approver_id: uuid.UUID,
    key: str,
    barrier: Barrier,
) -> str:
    with fixture.session_factory() as db:
        barrier.wait(timeout=10)
        try:
            result = approve_paid_invoice_period_repair(
                db,
                ApprovePaidInvoicePeriodRepairCommand(
                    request_id=request_id,
                    preview_fingerprint=fingerprint,
                    approved_by=approver_id,
                    permission_granted=True,
                ),
                context=_context(approver_id, key),
            )
        except PaidInvoicePeriodRepairError as exc:
            return exc.code.rsplit(".", maxsplit=1)[-1]
        return "replayed" if result.replayed else "applied"


def _assert_one_repair(fixture: _Fixture) -> None:
    with fixture.session_factory() as check:
        invoice = check.get(Invoice, fixture.invoice_id)
        assert invoice is not None
        assert invoice.billing_period_start is not None
        assert invoice.billing_period_end is not None
        assert invoice.status is InvoiceStatus.paid
        assert (
            check.query(ServiceEntitlement)
            .filter_by(subscription_id=fixture.subscription_id)
            .count()
            == 1
        )


def test_concurrent_approvals_of_one_request_converge(engine) -> None:
    fixture = _setup(engine)
    request_id, fingerprint = _request(fixture, f"pg-period-{uuid.uuid4().hex}")
    approver_id = fixture.approver_ids[0]
    barrier = Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda index: _approve(
                    fixture,
                    request_id=request_id,
                    fingerprint=fingerprint,
                    approver_id=approver_id,
                    key=f"pg-period-approve-{index}",
                    barrier=barrier,
                ),
                range(2),
            )
        )

    assert sorted(outcomes) == ["applied", "replayed"]
    _assert_one_repair(fixture)


def test_competing_requests_for_one_invoice_apply_once(engine) -> None:
    fixture = _setup(engine)
    first, fingerprint = _request(fixture, f"pg-period-a-{uuid.uuid4().hex}")
    second, second_fingerprint = _request(fixture, f"pg-period-b-{uuid.uuid4().hex}")
    assert first != second
    assert fingerprint == second_fingerprint
    barrier = Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda args: _approve(
                    fixture,
                    request_id=args[0],
                    fingerprint=fingerprint,
                    approver_id=args[1],
                    key=f"pg-period-approve-{args[0]}",
                    barrier=barrier,
                ),
                [(first, fixture.approver_ids[0]), (second, fixture.approver_ids[1])],
            )
        )

    assert sorted(outcomes) == ["applied", "stale_preview"]
    _assert_one_repair(fixture)
