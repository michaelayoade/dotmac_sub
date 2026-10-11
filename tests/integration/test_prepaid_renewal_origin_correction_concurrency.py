"""PostgreSQL serialization for the finance-reviewed renewal origin correction.

Confirmation locks account, adjustment, ledger entry, subscription, and
entitlements before recomputing its fingerprint, so concurrent confirmations
converge: with one idempotency key the second replays the stored outcome; with
two keys the second sees the already-canonical reference and is refused as
stale. Exactly one reference rewrite and one audit row exist afterwards, the
ledger debit and entitlement are untouched, and the active-ledger-entry unique
index arbitrates a competing debit link.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier

from sqlalchemy.orm import sessionmaker

from app.models.audit import AuditEvent
from app.models.billing import (
    AccountAdjustment,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
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
from app.services.prepaid_renewal_origin_correction import (
    CORRECTION_PERMISSION,
    CorrectRenewalOriginCommand,
    RenewalOriginCorrectionError,
    RenewalOriginCorrectionQuery,
    RenewalOriginDisposition,
    RenewalOriginWarning,
    canonical_origin_ref,
    correct_renewal_origin,
    preview_renewal_origin_correction,
)

PRICE = Decimal("17500.00")
DEBIT = Decimal("18812.50")


@dataclass(frozen=True)
class _Fixture:
    session_factory: sessionmaker
    adjustment_id: uuid.UUID
    subscription_id: uuid.UUID
    entitlement_id: uuid.UUID
    ledger_entry_id: uuid.UUID
    query: RenewalOriginCorrectionQuery
    staff_id: uuid.UUID
    start: datetime
    end: datetime


def _context(user_id: uuid.UUID, key: str) -> CommandContext:
    return CommandContext.system(
        actor=f"user:{user_id}",
        scope=CORRECTION_PERMISSION,
        reason="PostgreSQL renewal origin correction concurrency",
        idempotency_key=key,
    )


def _setup(engine) -> _Fixture:
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    start = datetime(2026, 7, 22, 7, 33, 48, 976393, tzinfo=UTC)
    end = datetime(2026, 8, 20, 7, 33, 48, 976393, tzinfo=UTC)
    with session_factory() as setup:
        reseller = Reseller(
            name=f"Origin Correction {suffix}",
            code=f"origin-correction-{suffix}",
            is_active=True,
        )
        account = Subscriber(
            first_name="Origin",
            last_name="Correction",
            email=f"origin-correction-{suffix}@example.com",
            reseller=reseller,
            status=SubscriberStatus.active,
            is_active=True,
            billing_enabled=True,
            billing_mode=BillingMode.prepaid,
        )
        offer = CatalogOffer(
            name=f"Origin Correction Offer {suffix}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            status=OfferStatus.active,
            is_active=True,
            billing_mode=BillingMode.prepaid,
        )
        staff = SystemUser(
            first_name="Finance",
            last_name="Operator",
            display_name="Finance Operator",
            email=f"finance-{suffix}@example.test",
            is_active=True,
        )
        setup.add_all([reseller, account, offer, staff])
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
            invoice_number=f"INV-PG-ORIGIN-{suffix}",
            status=InvoiceStatus.paid,
            currency="NGN",
            subtotal=PRICE,
            total=PRICE,
            balance_due=Decimal("0.00"),
            billing_period_start=start,
            billing_period_end=end,
            issued_at=start,
            paid_at=start,
        )
        ledger = LedgerEntry(
            account_id=account.id,
            entry_type=LedgerEntryType.debit,
            source=LedgerSource.adjustment,
            category=LedgerCategory.internet_service,
            amount=DEBIT,
            currency="NGN",
            memo="Prepaid service renewal",
            is_active=True,
            affects_customer_position=True,
        )
        setup.add_all([subscription, invoice, ledger])
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
        adjustment = AccountAdjustment(
            account_id=account.id,
            category=LedgerCategory.internet_service,
            amount=DEBIT,
            currency="NGN",
            memo="Prepaid service renewal",
            reason="Historical direct renewal",
            origin="prepaid_service_renewal",
            origin_ref=str(invoice.id),
            prepaid_funding_before=Decimal("20000.00"),
            prepaid_funding_after=Decimal("1187.50"),
            postpaid_receivables=Decimal("0.00"),
            collection_blocking_balance=Decimal("0.00"),
            access_consequence="none_adjustment_only",
            preview_fingerprint="a" * 64,
            idempotency_key=f"pg-origin-{suffix}",
            ledger_entry_id=ledger.id,
        )
        setup.add_all([line, adjustment])
        setup.flush()
        entitlement = ServiceEntitlement(
            account_id=account.id,
            subscription_id=subscription.id,
            source_invoice_id=invoice.id,
            source_invoice_line_id=line.id,
            source_ledger_entry_id=ledger.id,
            starts_at=start,
            ends_at=end,
            amount_funded=PRICE,
            currency="NGN",
        )
        setup.add(entitlement)
        setup.commit()
        query = RenewalOriginCorrectionQuery(
            adjustment_id=adjustment.id,
            disposition=RenewalOriginDisposition.entitlement_already_linked,
            entitlement_id=entitlement.id,
            acknowledged_warnings=(
                RenewalOriginWarning.entitlement_amount_differs_from_debit,
                RenewalOriginWarning.entitlement_invoice_backed,
            ),
        )
        return _Fixture(
            session_factory=session_factory,
            adjustment_id=adjustment.id,
            subscription_id=subscription.id,
            entitlement_id=entitlement.id,
            ledger_entry_id=ledger.id,
            query=query,
            staff_id=staff.id,
            start=start,
            end=end,
        )


def _fingerprint(fixture: _Fixture) -> str:
    with fixture.session_factory() as db:
        fingerprint = preview_renewal_origin_correction(db, fixture.query).fingerprint
        db.commit()
        return fingerprint


def _confirm(fixture: _Fixture, *, fingerprint: str, key: str, barrier: Barrier) -> str:
    with fixture.session_factory() as db:
        barrier.wait(timeout=10)
        try:
            result = correct_renewal_origin(
                db,
                CorrectRenewalOriginCommand(
                    query=fixture.query,
                    preview_fingerprint=fingerprint,
                    reason="Finance confirmed service was delivered; reference only",
                    evidence_reference="FIN-PG-2",
                    evidence_sha256="d" * 64,
                    corrected_by=fixture.staff_id,
                    permission_granted=True,
                ),
                context=_context(fixture.staff_id, key),
            )
        except RenewalOriginCorrectionError as exc:
            return exc.code.rsplit(".", maxsplit=1)[-1]
        return "replayed" if result.replayed else "applied"


def _assert_one_correction(fixture: _Fixture) -> None:
    with fixture.session_factory() as check:
        adjustment = check.get(AccountAdjustment, fixture.adjustment_id)
        assert adjustment is not None
        assert adjustment.origin_ref == canonical_origin_ref(
            fixture.subscription_id, fixture.start, fixture.end
        )
        assert adjustment.amount == DEBIT
        assert (
            check.query(ServiceEntitlement)
            .filter_by(subscription_id=fixture.subscription_id)
            .count()
            == 1
        )
        assert (
            check.query(LedgerEntry).filter_by(id=fixture.ledger_entry_id).count() == 1
        )
        assert (
            check.query(AuditEvent)
            .filter_by(
                action="correct_prepaid_renewal_origin_ref",
                entity_id=str(fixture.adjustment_id),
            )
            .count()
            == 1
        )


def test_concurrent_confirmations_with_one_key_converge(engine) -> None:
    fixture = _setup(engine)
    fingerprint = _fingerprint(fixture)
    key = f"pg-origin-{uuid.uuid4().hex}"
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
    _assert_one_correction(fixture)


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
                    key=f"pg-origin-{index}-{uuid.uuid4().hex}",
                    barrier=barrier,
                ),
                range(2),
            )
        )

    assert sorted(outcomes) == ["applied", "stale_preview"]
    _assert_one_correction(fixture)
