"""ADR 0032 incident acceptance tests at the Starter boundary (PG).

Derived from the prepaid paid-but-not-reactivated incident, these tests
pin the structural guarantees prepaid renewals depend on before that
lane may switch:

1. Cash confirmed against a payments intent bound to one invoice cannot
   discharge a different invoice — the settlement owner refuses the
   mismatched intent binding before any confirmation is recorded.
2. A full settlement is exactly-once under replay: identical provider
   evidence returns the same confirmation and settlement identities,
   leaves the Starter position discharged exactly once, and produces
   exactly one ``payment.received`` access signal.
3. Settlement never switches service state itself: discharging the
   invoice leaves the subscriber lifecycle status untouched; the single
   ``payment.received`` signal is the only output the access owner
   consumes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from dotmac_billing import (
    GetCurrentReceivablePositionV1,
    get_current_receivable_position,
)
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money
from dotmac_payments import OpenPaymentIntent, PaymentPurpose, open_payment_intent
from sqlalchemy import func, select

from app.models.billing import (
    Invoice,
    InvoiceDueDateBasis,
    InvoiceLine,
    InvoiceStatus,
    StarterInvoiceLink,
    TaxRate,
)
from app.models.catalog import BillingMode
from app.models.event_store import EventStore
from app.services import web_billing_invoices as web_invoices
from app.services.billing import starter_policies
from app.services.billing.starter_settlement import (
    StarterSettlementError,
    settle_starter_invoice_payment,
)
from app.services.brand_profiles import upsert_brand_profile
from app.services.events.types import EventType
from app.services.operator_tenant import OPERATOR_TENANT_ID

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("starter_native_vat_tax_policy"),
]

_SERIES = f"invacc-{uuid4().hex[:8]}"
_SCOPE = TenantScope(OPERATOR_TENANT_ID)


@pytest.fixture
def _starter_series_isolation(monkeypatch):
    real_resolve = starter_policies.resolve_value

    def _resolve(db, domain, key):
        if key == "starter_billing_invoice_series":
            return _SERIES
        if key == "starter_billing_invoice_series_prefix":
            return "ACC"
        if key == "starter_ledger_live_enabled":
            return True
        return real_resolve(db, domain, key)

    monkeypatch.setattr(starter_policies, "resolve_value", _resolve)


@pytest.fixture
def _seller_and_bank(db_session):
    from app.models.billing import CollectionAccount, CollectionAccountType

    upsert_brand_profile(
        db_session,
        scope_type="platform",
        scope_id=None,
        values={
            "legal_name": "Dotmac Technologies Ltd",
            "legal_address": {
                "street1": "Plot 1 Proof Way",
                "city": "Abuja",
                "country": "NGA",
            },
        },
    )
    db_session.add(
        CollectionAccount(
            name=f"Acceptance Collection {uuid4().hex[:8]}",
            account_type=CollectionAccountType.bank,
            bank_name="Dotmac Bank",
            account_name="Dotmac Technologies Ltd",
            account_number="0123456789",
            account_last4="6789",
            sort_code="12-34-56",
            currency="NGN",
            is_active=True,
        )
    )
    db_session.commit()


def _issued_invoice(
    db_session, subscriber, tax_rate, *, total: str = "107.50"
) -> tuple[Invoice, StarterInvoiceLink]:
    amount = Decimal(total)
    net = (amount / Decimal("1.075")).quantize(Decimal("0.01"))
    tax = amount - net
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.draft,
        currency="NGN",
        subtotal=net,
        tax_total=tax,
        total=amount,
        balance_due=amount,
        discount_amount=Decimal("0.00"),
        due_at=datetime.now(UTC) + timedelta(days=30),
        due_date_basis=InvoiceDueDateBasis.contract_terms,
        due_date_basis_ref="adr0032-acceptance-terms",
        due_date_policy_version="1",
        is_proforma=False,
    )
    invoice.lines = [
        InvoiceLine(
            description="Fiber access (monthly)",
            quantity=Decimal("1"),
            unit_price=net,
            amount=net,
            tax_rate_id=tax_rate.id,
            tax_rate_code_snapshot="vat_standard",
            tax_rate_percent_snapshot=Decimal("7.5000"),
            tax_rate_snapshot_version=1,
            tax_rate_is_active_snapshot=True,
            is_active=True,
        )
    ]
    db_session.add(invoice)
    db_session.commit()
    issued = web_invoices.issue_invoice_from_detail(
        db_session, invoice_id=invoice.id, actor_ref="user:adr0032-acceptance"
    )
    link = db_session.execute(
        select(StarterInvoiceLink).where(StarterInvoiceLink.invoice_id == invoice.id)
    ).scalar_one()
    return issued, link


def _setup_invoice(db_session, subscriber) -> tuple[Invoice, StarterInvoiceLink]:
    subscriber.billing_mode = BillingMode.postpaid
    subscriber.billing_address_line1 = "2 Customer Close"
    subscriber.billing_city = "Abuja"
    tax_rate = TaxRate(
        name=f"Acceptance VAT {uuid4().hex[:8]}",
        code="vat_standard",
        rate=Decimal("0.0750"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.flush()
    return _issued_invoice(db_session, subscriber, tax_rate)


def _open_intent(db_session, subscriber, invoice, amount: str) -> str:
    reference = f"adr0032:{uuid4().hex[:12]}"
    now = datetime.now(UTC)
    open_payment_intent(
        db_session,
        scope=_SCOPE,
        command=OpenPaymentIntent(
            reference=reference,
            payer_reference=str(subscriber.id),
            purpose=PaymentPurpose.INVOICE_SETTLEMENT,
            target_reference=str(invoice.id),
            requested=Money(Decimal(amount), Currency("NGN", 2)),
            provider_type="paystack",
            channel="customer_selfcare",
            opened_at=now,
            expires_at=now + timedelta(hours=24),
        ),
    )
    db_session.flush()
    return reference


def _collectible(db_session, link: StarterInvoiceLink) -> Decimal:
    current = get_current_receivable_position(
        db_session,
        scope=_SCOPE,
        request=GetCurrentReceivablePositionV1(
            scope=_SCOPE,
            billing_account_id=link.billing_account_id,
            currency=Currency("NGN", 2),
        ),
    )
    assert current.position is not None
    return current.position.collectible_receivable.amount


def _payment_received_count(db_session, invoice_id) -> int:
    return int(
        db_session.execute(
            select(func.count())
            .select_from(EventStore)
            .where(
                EventStore.event_type == EventType.payment_received.value,
                EventStore.invoice_id == invoice_id,
            )
        ).scalar_one()
    )


def test_intent_bound_cash_cannot_settle_another_invoice(
    db_session, subscriber, _seller_and_bank, _starter_series_isolation
) -> None:
    subscriber.billing_mode = BillingMode.postpaid
    invoice_a, link_a = _setup_invoice(db_session, subscriber)
    tax_id = invoice_a.lines[0].tax_rate_id
    tax_rate = db_session.get(TaxRate, tax_id)
    invoice_b, link_b = _issued_invoice(db_session, subscriber, tax_rate)
    assert link_a.billing_account_id == link_b.billing_account_id
    assert _collectible(db_session, link_a) == Decimal("215.00")

    reference_a = _open_intent(db_session, subscriber, invoice_a, "107.50")
    now = datetime.now(UTC)

    with pytest.raises(StarterSettlementError, match="intent"):
        settle_starter_invoice_payment(
            db_session,
            invoice=invoice_b,
            link=link_b,
            intent_reference=reference_a,
            external_reference=f"paystack-txn-{uuid4().hex[:12]}",
            confirmed_amount=Decimal("107.50"),
            occurred_at=now,
            observed_at=now,
        )
    db_session.rollback()
    db_session.refresh(invoice_b)

    assert invoice_b.status is InvoiceStatus.issued
    assert invoice_b.balance_due == Decimal("107.50")
    assert _collectible(db_session, link_b) == Decimal("215.00")
    assert _payment_received_count(db_session, invoice_b.id) == 0


def test_full_settlement_replay_is_exactly_once(
    db_session, subscriber, _seller_and_bank, _starter_series_isolation
) -> None:
    invoice, link = _setup_invoice(db_session, subscriber)
    status_before = subscriber.status
    reference = _open_intent(db_session, subscriber, invoice, "107.50")
    external_reference = f"paystack-txn-{uuid4().hex[:12]}"
    now = datetime.now(UTC)
    kwargs = dict(
        invoice=invoice,
        link=link,
        intent_reference=reference,
        external_reference=external_reference,
        confirmed_amount=Decimal("107.50"),
        occurred_at=now,
        observed_at=now,
    )

    result = settle_starter_invoice_payment(db_session, **kwargs)
    db_session.commit()
    replay = settle_starter_invoice_payment(db_session, **kwargs)
    db_session.commit()
    db_session.refresh(invoice)
    db_session.refresh(subscriber)

    assert replay.confirmation_id == result.confirmation_id
    assert replay.settlement_id == result.settlement_id
    assert _collectible(db_session, link) == Decimal("0.00")
    assert invoice.status is InvoiceStatus.paid
    assert _payment_received_count(db_session, invoice.id) == 1
    # Settlement emits the signal only; it never switches service state.
    assert subscriber.status == status_before
