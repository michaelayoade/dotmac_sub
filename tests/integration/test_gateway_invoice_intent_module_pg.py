"""Invoice checkout opens the Starter payments intent (migrated PostgreSQL).

Thin-client migration slice (ADR 0020): creating a customer invoice-payment
gateway intent now also opens the ``dotmac-payments`` intent in the same
root transaction, with the exact authorized binding. This proves, against
the real migrated schema, that the module intent exists and the existing
parity observation reports an exact MATCH — the legacy ``TopupIntent`` is
now a projection of the module-owned intent at creation time. Settlement
still records against the legacy intent until invoices are Starter-issued.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from dotmac_kernel.cache import TenantScope
from dotmac_payments import GetPaymentIntentV1, PaymentPurpose, get_payment_intent_v1

from app.models.billing import Invoice, InvoiceStatus, TopupIntent
from app.services import gateway_topup_intents as svc
from app.services.db_session_adapter import db_session_adapter
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import CommandContext
from app.services.payment_intent_module_shadow import (
    IntentParityOutcome,
    authorized_invoice_binding,
    observe_invoice_intent_parity,
)
from tests.integration_platform_helpers import enable_payment_provider

pytestmark = pytest.mark.integration


def test_invoice_checkout_opens_matching_module_intent(
    monkeypatch, db_session, subscriber
) -> None:
    monkeypatch.setattr(
        svc,
        "resolve_value",
        lambda _db, _domain, key: {
            "gateway_topup_intent_ttl_minutes": 30,
            "topup_min_amount": 1000,
            "topup_max_amount": 500000,
        }[key],
    )
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number=f"INV-MODULE-PG-{uuid4().hex[:8]}",
        currency="NGN",
        subtotal=Decimal("2400.00"),
        total=Decimal("2400.00"),
        balance_due=Decimal("2400.00"),
        status=InvoiceStatus.issued,
    )
    db_session.add(invoice)
    db_session.commit()
    binding_id = enable_payment_provider(db_session, "paystack")[
        "payments.intent.v1"
    ].id
    reference = f"module-pg-{uuid4()}"

    db_session_adapter.release_read_transaction(db_session)
    result = svc.create_customer_gateway_topup_intent(
        db_session,
        svc.CreateCustomerGatewayTopupIntentCommand(
            flow=svc.CustomerGatewayTopupFlow.invoice_payment,
            account_id=subscriber.id,
            invoice_id=invoice.id,
            reference=reference,
            provider_type="paystack",
            provider_id=None,
            created_by="pytest:module-intent-pg",
            capability_binding_id=binding_id,
        ),
        context=CommandContext.system(
            actor="pytest:module-intent-pg",
            scope=svc.CREATE_CUSTOMER_SCOPE,
            reason="Module intent composition proof",
        ),
    )

    intent = db_session.get(TopupIntent, result.intent_id)
    assert intent is not None
    observed = get_payment_intent_v1(
        db_session,
        scope=TenantScope(OPERATOR_TENANT_ID),
        query=GetPaymentIntentV1(reference=reference),
    )
    assert observed is not None
    assert observed.purpose is PaymentPurpose.INVOICE_SETTLEMENT
    assert observed.target_reference == str(invoice.id)
    assert observed.payer_reference == str(subscriber.id)
    assert observed.requested.amount == Decimal("2400.00")

    parity = observe_invoice_intent_parity(
        db_session, authorized_invoice_binding(intent)
    )
    assert parity.outcome is IntentParityOutcome.MATCH, parity.mismatched_fields
