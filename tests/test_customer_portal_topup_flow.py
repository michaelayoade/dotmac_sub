import importlib
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.billing import (
    CreditNote,
    CreditNoteStatus,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentProvider,
    PaymentProviderType,
    PaymentStatus,
    TopupIntent,
)
from app.models.subscriber import Subscriber
from app.schemas.billing import InvoiceCreate, PaymentMethodCreate
from app.services import billing as billing_service
from app.services.customer_portal_flow_billing import get_billing_page
from app.services.customer_portal_flow_payments import (
    create_invoice_payment_intent,
    create_topup_intent,
    get_topup_page,
    verify_and_record_payment,
    verify_and_record_topup,
)


def _make_invoice(
    db_session, account_id, *, amount: str, invoice_number: str
) -> object:
    return billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=account_id,
            invoice_number=invoice_number,
            currency="NGN",
            subtotal=Decimal(amount),
            total=Decimal(amount),
            balance_due=Decimal(amount),
            status=InvoiceStatus.issued,
        ),
    )


def _patch_topup_settings(
    monkeypatch,
    *,
    min_amount: int = 1000,
    max_amount: int = 500000,
    preset_amounts: str | None = None,
) -> None:
    def _fake_resolve_value(_db, _domain, key):
        if key == "topup_min_amount":
            return min_amount
        if key == "topup_max_amount":
            return max_amount
        if key == "topup_preset_amounts":
            return preset_amounts
        return None

    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.resolve_value",
        _fake_resolve_value,
    )


def _create_intent(
    monkeypatch, db_session, subscriber, *, amount: str, reference: str
) -> dict:
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.build_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider_type="paystack",
            public_key="pk_test_topup",
            reference=reference,
        ),
    )
    return create_topup_intent(
        db_session,
        {"account_id": str(subscriber.id), "username": "customer@example.com"},
        amount,
        provider="paystack",
    )


def test_get_topup_page_includes_limits_and_public_key(
    monkeypatch, db_session, subscriber
):
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.build_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider_type="paystack",
            public_key="pk_test_topup",
            reference="unused-ref",
        ),
    )
    _patch_topup_settings(monkeypatch, min_amount=2500, max_amount=750000)

    page = get_topup_page(
        db_session,
        {"account_id": str(subscriber.id), "username": "customer@example.com"},
    )

    assert "payment_reference" not in page
    assert page["provider_public_key"] == "pk_test_topup"
    assert page["min_amount"] == 2500
    assert page["max_amount"] == 750000
    assert page["payment_options"] == [
        {"provider_type": "paystack", "label": "Pay with Paystack"},
    ]


def test_get_topup_page_uses_configured_preset_amounts_within_limits(
    monkeypatch, db_session, subscriber
):
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.build_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider_type="paystack",
            public_key="pk_test_topup",
            reference="unused-ref",
        ),
    )
    _patch_topup_settings(
        monkeypatch,
        min_amount=2500,
        max_amount=12000,
        preset_amounts="1000,3000,7000,7000,20000",
    )

    page = get_topup_page(
        db_session,
        {"account_id": str(subscriber.id), "username": "customer@example.com"},
    )

    assert page["preset_amounts"] == [3000, 7000]


def test_get_topup_page_includes_saved_payment_methods(
    monkeypatch, db_session, subscriber
):
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.build_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider_type="paystack",
            public_key="pk_test_topup",
            reference="unused-ref",
        ),
    )
    _patch_topup_settings(monkeypatch)
    card = billing_service.payment_methods.create(
        db_session,
        PaymentMethodCreate(
            account_id=subscriber.id,
            label="Visa •••• 4081",
            token="AUTH_4081",
            last4="4081",
            brand="visa",
            is_default=True,
        ),
    )

    page = get_topup_page(
        db_session,
        {"account_id": str(subscriber.id), "username": "customer@example.com"},
    )

    assert [str(method.id) for method in page["payment_methods"]] == [str(card.id)]


def test_get_topup_page_surfaces_active_flutterwave_provider(
    monkeypatch, db_session, subscriber
):
    db_session.add_all(
        [
            PaymentProvider(
                name="Paystack",
                provider_type=PaymentProviderType.paystack,
                is_active=True,
            ),
            PaymentProvider(
                name="Flutterwave",
                provider_type=PaymentProviderType.flutterwave,
                is_active=True,
            ),
            PaymentProvider(
                name="Manual",
                provider_type=PaymentProviderType.manual,
                is_active=True,
            ),
            PaymentProvider(
                name="Disabled Flutterwave",
                provider_type=PaymentProviderType.flutterwave,
                is_active=False,
            ),
        ]
    )
    db_session.commit()
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.build_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider_type="paystack",
            public_key="pk_test_topup",
            reference="unused-ref",
        ),
    )
    _patch_topup_settings(monkeypatch)

    page = get_topup_page(
        db_session,
        {"account_id": str(subscriber.id), "username": "customer@example.com"},
    )

    # Paystack (the default) is listed first; an active Flutterwave provider is
    # now surfaced too. Manual and the inactive Flutterwave row are excluded.
    assert page["payment_options"] == [
        {"provider_type": "paystack", "label": "Pay with Paystack"},
        {"provider_type": "flutterwave", "label": "Pay with Flutterwave"},
    ]


def test_get_topup_page_omits_balance_when_lookup_fails(
    monkeypatch, db_session, subscriber
):
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.build_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider_type="paystack",
            public_key="pk_test_topup",
            reference="unused-ref",
        ),
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.get_available_balance",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("lookup failed")),
    )
    _patch_topup_settings(monkeypatch)

    page = get_topup_page(
        db_session,
        {"account_id": str(subscriber.id), "username": "customer@example.com"},
    )

    assert page["prepaid_balance"] is None


def test_get_billing_page_includes_current_balance(monkeypatch, db_session, subscriber):
    monkeypatch.setattr(
        "app.services.customer_portal_flow_billing.get_available_balance",
        lambda *_args, **_kwargs: Decimal("2500.00"),
    )

    page = get_billing_page(
        db_session,
        {"account_id": str(subscriber.id), "username": "customer@example.com"},
    )

    assert page["prepaid_balance"] == Decimal("2500.00")


def test_get_billing_page_includes_payments_and_credit_notes_in_activity(
    monkeypatch, db_session, subscriber
):
    monkeypatch.setattr(
        "app.services.customer_portal_flow_billing.get_available_balance",
        lambda *_args, **_kwargs: Decimal("2500.00"),
    )
    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("1015.23"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        memo="Paystack prepaid top-up ref: DMAC-test",
        is_active=True,
    )
    credit_note = CreditNote(
        account_id=subscriber.id,
        credit_number="CN-test",
        status=CreditNoteStatus.issued,
        currency="NGN",
        total=Decimal("1500.00"),
        memo="Test credit",
        is_active=True,
    )
    payment_ledger = LedgerEntry(
        account_id=subscriber.id,
        payment=payment,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=Decimal("1015.23"),
        currency="NGN",
        memo="Duplicate payment ledger",
        is_active=True,
    )
    adjustment = LedgerEntry(
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.adjustment,
        amount=Decimal("500.00"),
        currency="NGN",
        memo="Manual adjustment",
        is_active=True,
    )
    db_session.add_all([payment, credit_note, payment_ledger, adjustment])
    db_session.commit()

    page = get_billing_page(
        db_session,
        {"account_id": str(subscriber.id), "username": "customer@example.com"},
    )

    activity = page["billing_activity"]
    amounts = [item.amount for item in activity]
    titles = [item.title for item in activity]

    assert Decimal("1015.23") in amounts
    assert Decimal("1500.00") in amounts
    assert Decimal("500.00") in amounts
    assert "Credit added" in titles
    assert "Duplicate payment ledger" not in titles


def test_get_billing_page_activity_excludes_failed_and_canceled_payments(
    monkeypatch, db_session, subscriber
):
    monkeypatch.setattr(
        "app.services.customer_portal_flow_billing.get_available_balance",
        lambda *_args, **_kwargs: Decimal("2500.00"),
    )
    failed = Payment(
        account_id=subscriber.id,
        amount=Decimal("188125.00"),
        currency="NGN",
        status=PaymentStatus.failed,
        memo="Pay by Paystack",
        is_active=True,
    )
    canceled = Payment(
        account_id=subscriber.id,
        amount=Decimal("99999.00"),
        currency="NGN",
        status=PaymentStatus.canceled,
        memo="Abandoned checkout",
        is_active=True,
    )
    succeeded = Payment(
        account_id=subscriber.id,
        amount=Decimal("17500.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        memo="Paid by Paystack",
        is_active=True,
    )
    db_session.add_all([failed, canceled, succeeded])
    db_session.commit()

    page = get_billing_page(
        db_session,
        {"account_id": str(subscriber.id), "username": "customer@example.com"},
    )

    amounts = [item.amount for item in page["billing_activity"]]
    assert Decimal("17500.00") in amounts
    assert Decimal("188125.00") not in amounts
    assert Decimal("99999.00") not in amounts


def test_create_topup_intent_persists_server_owned_reference(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch)
    payload = _create_intent(
        monkeypatch,
        db_session,
        subscriber,
        amount="5000.00",
        reference="topup-intent-ref-1",
    )

    intent = (
        db_session.query(TopupIntent).filter_by(reference="topup-intent-ref-1").one()
    )

    assert payload["reference"] == "topup-intent-ref-1"
    assert payload["checkout_metadata"]["topup_intent_id"] == str(intent.id)
    assert payload["checkout_metadata"]["account_id"] == str(subscriber.id)
    assert intent.requested_amount == Decimal("5000.00")
    assert intent.status == "pending"


def test_create_topup_intent_records_selected_payment_method(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch)
    card = billing_service.payment_methods.create(
        db_session,
        PaymentMethodCreate(
            account_id=subscriber.id,
            label="Visa •••• 4081",
            token="AUTH_4081",
            last4="4081",
            brand="visa",
            is_default=True,
        ),
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.build_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider_type="paystack",
            public_key="pk_test_topup",
            reference="topup-intent-ref-card",
        ),
    )
    captured_charge = {}

    def fake_charge_authorization(_db, **kwargs):
        captured_charge.update(kwargs)
        return {"status": "success", "reference": kwargs["reference"]}

    monkeypatch.setattr(
        "app.services.paystack.charge_authorization",
        fake_charge_authorization,
    )

    payload = create_topup_intent(
        db_session,
        {"account_id": str(subscriber.id), "username": "customer@example.com"},
        "5000.00",
        provider="paystack",
        payment_method_id=str(card.id),
    )

    intent = (
        db_session.query(TopupIntent).filter_by(reference="topup-intent-ref-card").one()
    )
    assert intent.metadata_["payment_method_id"] == str(card.id)
    assert payload["checkout_metadata"]["payment_method_id"] == str(card.id)
    assert payload["charged"] is True
    assert captured_charge["authorization_code"] == "AUTH_4081"
    assert captured_charge["reference"] == "topup-intent-ref-card"
    assert captured_charge["metadata"]["payment_method_id"] == str(card.id)


def test_create_topup_intent_initializes_flutterwave_checkout(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch)
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.build_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider_type="flutterwave",
            public_key="flw_pk_test",
            reference="topup-intent-ref-flw",
        ),
    )
    captured_checkout = {}

    def fake_initialize_transaction(_db, **kwargs):
        captured_checkout.update(kwargs)
        return {"link": "https://checkout.flutterwave.test/pay/topup-intent-ref-flw"}

    monkeypatch.setattr(
        "app.services.flutterwave.initialize_transaction",
        fake_initialize_transaction,
    )

    payload = create_topup_intent(
        db_session,
        {"account_id": str(subscriber.id), "username": "customer@example.com"},
        "5000.00",
        provider="flutterwave",
        redirect_url="https://selfcare.test/portal/billing/topup/verify",
    )

    assert payload["provider_type"] == "flutterwave"
    assert (
        payload["checkout_url"]
        == "https://checkout.flutterwave.test/pay/topup-intent-ref-flw"
    )
    assert captured_checkout["email"] == subscriber.email
    assert captured_checkout["reference"] == "topup-intent-ref-flw"
    assert captured_checkout["redirect_url"] == (
        "https://selfcare.test/portal/billing/topup/verify"
        "?reference=topup-intent-ref-flw&provider=flutterwave"
    )
    assert captured_checkout["metadata"]["payment_flow"] == "account_topup"
    assert captured_checkout["metadata"]["account_id"] == str(subscriber.id)


def test_create_topup_intent_rejects_gateway_when_customer_email_blank(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch)
    subscriber.email = ""
    db_session.add(subscriber)
    db_session.commit()

    with pytest.raises(ValueError, match="email address"):
        create_topup_intent(
            db_session,
            {"account_id": str(subscriber.id), "username": "pppoe-login"},
            "5000.00",
            provider="paystack",
        )


# ---------------------------------------------------------------------------
# Invoice payment chooser (create_invoice_payment_intent)
# ---------------------------------------------------------------------------


def _invoice_customer(subscriber) -> dict:
    return {"account_id": str(subscriber.id), "username": "customer@example.com"}


def test_create_invoice_payment_intent_gateway_paystack(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="2500.00", invoice_number="INV-PAY-1"
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.build_context",
        lambda *_a, **_k: SimpleNamespace(
            provider_type="paystack", public_key="pk_test_pay", reference="pay-ref-1"
        ),
    )

    payload = create_invoice_payment_intent(
        db_session, _invoice_customer(subscriber), str(invoice.id), provider="paystack"
    )

    assert payload["charged"] is False
    assert payload["checkout_url"] is None
    assert payload["reference"] == "pay-ref-1"
    assert payload["provider_public_key"] == "pk_test_pay"
    assert payload["amount"] == Decimal("2500.00")
    assert payload["checkout_metadata"]["invoice_id"] == str(invoice.id)
    assert payload["checkout_metadata"]["payment_flow"] == "invoice_payment"


def test_create_invoice_payment_intent_charges_saved_card(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="3000.00", invoice_number="INV-PAY-2"
    )
    card = billing_service.payment_methods.create(
        db_session,
        PaymentMethodCreate(
            account_id=subscriber.id,
            label="Visa •••• 4081",
            token="AUTH_4081",
            last4="4081",
            brand="visa",
            is_default=True,
        ),
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.build_context",
        lambda *_a, **_k: SimpleNamespace(
            provider_type="paystack", public_key="pk_test_pay", reference="pay-ref-card"
        ),
    )
    captured = {}

    def fake_charge(_db, **kwargs):
        captured.update(kwargs)
        return {"status": "success", "reference": kwargs["reference"]}

    monkeypatch.setattr("app.services.paystack.charge_authorization", fake_charge)

    payload = create_invoice_payment_intent(
        db_session,
        _invoice_customer(subscriber),
        str(invoice.id),
        provider="paystack",
        payment_method_id=str(card.id),
        idempotency_key="idem-pay-1",
    )

    assert payload["charged"] is True
    assert payload["reference"] == "pay-ref-card"
    assert captured["authorization_code"] == "AUTH_4081"
    assert captured["amount_kobo"] == 300000
    assert captured["metadata"]["invoice_id"] == str(invoice.id)


def test_create_invoice_payment_intent_saved_card_idempotent_replay(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="3000.00", invoice_number="INV-PAY-3"
    )
    card = billing_service.payment_methods.create(
        db_session,
        PaymentMethodCreate(
            account_id=subscriber.id,
            label="Visa •••• 1111",
            token="AUTH_1111",
            last4="1111",
            brand="visa",
            is_default=True,
        ),
    )
    refs = iter(["ref-A", "ref-B"])
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.build_context",
        lambda *_a, **_k: SimpleNamespace(
            provider_type="paystack", public_key="pk", reference=next(refs)
        ),
    )
    charges: list[dict] = []
    monkeypatch.setattr(
        "app.services.paystack.charge_authorization",
        lambda _db, **kw: charges.append(kw) or {"status": "success"},
    )
    cust = _invoice_customer(subscriber)

    first = create_invoice_payment_intent(
        db_session,
        cust,
        str(invoice.id),
        provider="paystack",
        payment_method_id=str(card.id),
        idempotency_key="dup-key",
    )
    second = create_invoice_payment_intent(
        db_session,
        cust,
        str(invoice.id),
        provider="paystack",
        payment_method_id=str(card.id),
        idempotency_key="dup-key",
    )

    # The card is charged exactly once; the replay reuses the first reference.
    assert len(charges) == 1
    assert first["reference"] == "ref-A"
    assert second["reference"] == "ref-A"
    assert second["replayed"] is True


def test_create_invoice_payment_intent_bank_transfer_hands_off(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="2500.00", invoice_number="INV-PAY-4"
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.direct_bank_transfer_enabled",
        lambda _db: True,
    )

    payload = create_invoice_payment_intent(
        db_session,
        _invoice_customer(subscriber),
        str(invoice.id),
        provider="direct_bank_transfer",
    )

    assert payload["provider_type"] == "direct_bank_transfer"
    assert payload["redirect_url"] == "/portal/billing/topup/transfer"
    assert payload["requested_amount"] == Decimal("2500.00")
    # The pending transfer intent is tagged with the invoice so the proof is
    # traceable back to it.
    intent = db_session.scalars(
        select(TopupIntent).where(TopupIntent.reference == payload["reference"])
    ).first()
    assert intent.metadata_["invoice_id"] == str(invoice.id)
    assert intent.metadata_["payment_flow"] == "invoice_payment"


def test_create_invoice_payment_intent_bank_transfer_allows_below_topup_min(
    monkeypatch, db_session, subscriber
):
    # A real invoice can be below the top-up minimum (e.g. a small fee); paying
    # it by transfer must not be blocked by the top-up limit.
    _patch_topup_settings(monkeypatch, min_amount=1000)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="500.00", invoice_number="INV-PAY-SMALL"
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.direct_bank_transfer_enabled",
        lambda _db: True,
    )

    payload = create_invoice_payment_intent(
        db_session,
        _invoice_customer(subscriber),
        str(invoice.id),
        provider="direct_bank_transfer",
    )

    assert payload["requested_amount"] == Decimal("500.00")
    assert payload["redirect_url"] == "/portal/billing/topup/transfer"


def test_create_invoice_payment_intent_rejects_gateway_when_customer_email_blank(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch)
    subscriber.email = ""
    db_session.add(subscriber)
    db_session.commit()
    invoice = _make_invoice(
        db_session, subscriber.id, amount="2500.00", invoice_number="INV-PAY-NOEMAIL"
    )

    with pytest.raises(ValueError, match="email address"):
        create_invoice_payment_intent(
            db_session,
            {"account_id": str(subscriber.id), "username": "pppoe-login"},
            str(invoice.id),
            provider="paystack",
        )


def test_create_invoice_payment_intent_records_and_completes_gateway_trace(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="2500.00", invoice_number="INV-PAY-TRACE"
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.build_context",
        lambda *_a, **_k: SimpleNamespace(
            provider_type="paystack", public_key="pk", reference="pay-ref-trace"
        ),
    )

    create_invoice_payment_intent(
        db_session, _invoice_customer(subscriber), str(invoice.id), provider="paystack"
    )

    # A durable pending record exists for the started checkout.
    intent = db_session.scalars(
        select(TopupIntent).where(TopupIntent.reference == "pay-ref-trace")
    ).first()
    assert intent is not None
    assert intent.status == "pending"
    assert intent.metadata_["invoice_id"] == str(invoice.id)

    # After verify, the trace is marked completed (idempotent, no-op if missing).
    from app.services.customer_portal_flow_payments import (
        complete_invoice_payment_intent,
    )

    payment = Payment(
        account_id=subscriber.id,
        amount=Decimal("2500.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
    )
    db_session.add(payment)
    db_session.commit()
    db_session.refresh(payment)

    complete_invoice_payment_intent(db_session, "pay-ref-trace", payment)
    db_session.refresh(intent)
    assert intent.status == "completed"
    assert intent.completed_payment_id == payment.id

    # No-op for an unknown reference.
    complete_invoice_payment_intent(db_session, "no-such-ref", payment)


def test_create_invoice_payment_intent_initializes_flutterwave(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="2500.00", invoice_number="INV-PAY-5"
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.build_context",
        lambda *_a, **_k: SimpleNamespace(
            provider_type="flutterwave",
            public_key="flw_pk_test",
            reference="pay-ref-flw",
        ),
    )
    captured = {}

    def fake_initialize_transaction(_db, **kwargs):
        captured.update(kwargs)
        return {"link": "https://checkout.flutterwave.test/pay/pay-ref-flw"}

    monkeypatch.setattr(
        "app.services.flutterwave.initialize_transaction", fake_initialize_transaction
    )

    payload = create_invoice_payment_intent(
        db_session,
        _invoice_customer(subscriber),
        str(invoice.id),
        provider="flutterwave",
        redirect_url="https://selfcare.test/portal/billing/pay/verify",
    )

    assert payload["provider_type"] == "flutterwave"
    assert (
        payload["checkout_url"] == "https://checkout.flutterwave.test/pay/pay-ref-flw"
    )
    assert captured["redirect_url"] == (
        "https://selfcare.test/portal/billing/pay/verify"
        "?reference=pay-ref-flw&provider=flutterwave"
    )
    assert captured["metadata"]["invoice_id"] == str(invoice.id)


def test_create_invoice_payment_intent_rejects_saved_card_non_paystack(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="2500.00", invoice_number="INV-PAY-6"
    )
    card = billing_service.payment_methods.create(
        db_session,
        PaymentMethodCreate(
            account_id=subscriber.id,
            label="Visa •••• 2222",
            token="AUTH_2222",
            last4="2222",
            brand="visa",
        ),
    )

    with pytest.raises(ValueError, match="Saved cards can only be used with Paystack"):
        create_invoice_payment_intent(
            db_session,
            _invoice_customer(subscriber),
            str(invoice.id),
            provider="flutterwave",
            payment_method_id=str(card.id),
        )


def test_create_invoice_payment_intent_rejects_paid_invoice(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="2500.00", invoice_number="INV-PAY-7"
    )
    invoice.status = InvoiceStatus.paid
    db_session.commit()

    with pytest.raises(ValueError, match="no longer payable"):
        create_invoice_payment_intent(
            db_session,
            _invoice_customer(subscriber),
            str(invoice.id),
            provider="paystack",
        )


def test_create_topup_intent_rejects_payment_method_for_other_account(
    monkeypatch, db_session, subscriber
):
    stranger = Subscriber(first_name="Other", last_name="User", email="o@x.io")
    db_session.add(stranger)
    db_session.commit()
    card = billing_service.payment_methods.create(
        db_session,
        PaymentMethodCreate(
            account_id=stranger.id,
            label="Visa •••• 9999",
            token="AUTH_9999",
            last4="9999",
            brand="visa",
        ),
    )
    _patch_topup_settings(monkeypatch)
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.build_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider_type="paystack",
            public_key="pk_test_topup",
            reference="topup-intent-ref-other-card",
        ),
    )

    with pytest.raises(ValueError, match="Payment method not found"):
        create_topup_intent(
            db_session,
            {"account_id": str(subscriber.id), "username": "customer@example.com"},
            "5000.00",
            provider="paystack",
            payment_method_id=str(card.id),
        )


def test_verify_and_record_topup_returns_allocation_breakdown_and_credit_added(
    monkeypatch, db_session, subscriber
):
    invoice = _make_invoice(
        db_session,
        subscriber.id,
        amount="3000.00",
        invoice_number="INV-TOPUP-1",
    )
    _patch_topup_settings(monkeypatch)
    intent = _create_intent(
        monkeypatch,
        db_session,
        subscriber,
        amount="5000.00",
        reference="ref-topup-1",
    )

    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.verify",
        lambda *_args, **_kwargs: SimpleNamespace(
            amount=Decimal("5000.00"),
            currency="NGN",
            external_id="ext-topup-1",
            memo_prefix="Paystack",
            metadata={"topup_intent_id": intent["intent_id"]},
        ),
    )
    monkeypatch.setattr("app.services.events.emit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.restore_account_services",
        lambda *_args, **_kwargs: 1,
    )

    result = verify_and_record_topup(
        db_session,
        {"account_id": str(subscriber.id)},
        "ref-topup-1",
        provider="paystack",
    )

    db_session.refresh(invoice)

    assert result["already_recorded"] is False
    assert result["allocated_total"] == Decimal("3000.00")
    assert result["credit_added"] == Decimal("2000.00")
    assert result["available_balance"] == Decimal("2000.00")
    assert result["policy_warnings"] == []
    assert result["allocated_to_invoices"] == [
        {
            "invoice_id": str(invoice.id),
            "invoice_number": "INV-TOPUP-1",
            "amount": Decimal("3000.00"),
        }
    ]
    assert invoice.balance_due == Decimal("0.00")


def test_verify_and_record_payment_allocates_invoice_and_credits_remainder(
    monkeypatch, db_session, subscriber
):
    invoice = _make_invoice(
        db_session,
        subscriber.id,
        amount="3000.00",
        invoice_number="INV-PAY-1",
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.verify",
        lambda *_args, **_kwargs: SimpleNamespace(
            amount=Decimal("5000.00"),
            currency="NGN",
            external_id="ext-pay-1",
            memo_prefix="Paystack",
            metadata={"invoice_id": str(invoice.id)},
        ),
    )
    payments_service = importlib.import_module("app.services.billing.payments")
    monkeypatch.setattr(payments_service, "emit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.services.collections.restore_account_services",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.get_available_balance",
        lambda *_args, **_kwargs: Decimal("2000.00"),
    )

    result = verify_and_record_payment(
        db_session,
        {"account_id": str(subscriber.id)},
        "ref-pay-1",
        provider="paystack",
    )

    db_session.refresh(invoice)
    payment = db_session.query(Payment).filter_by(external_id="ext-pay-1").one()

    assert result["already_recorded"] is False
    assert result["allocated_total"] == Decimal("3000.00")
    assert result["credit_added"] == Decimal("2000.00")
    assert result["available_balance"] == Decimal("2000.00")
    assert result["allocated_to_invoices"] == [
        {
            "invoice_id": str(invoice.id),
            "invoice_number": "INV-PAY-1",
            "amount": Decimal("3000.00"),
        }
    ]
    assert payment.amount == Decimal("5000.00")
    assert invoice.balance_due == Decimal("0.00")


def test_verify_and_record_topup_rejects_reference_for_other_customer(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch)
    _create_intent(
        monkeypatch,
        db_session,
        subscriber,
        amount="5000.00",
        reference="ref-topup-owned",
    )
    other_customer = Subscriber(
        first_name="Other",
        last_name="User",
        email="other@example.com",
    )
    db_session.add(other_customer)
    db_session.commit()
    db_session.refresh(other_customer)

    with pytest.raises(ValueError, match="does not belong to this account"):
        verify_and_record_topup(
            db_session,
            {"account_id": str(other_customer.id)},
            "ref-topup-owned",
            provider="paystack",
        )


def test_verify_and_record_topup_records_out_of_policy_charge_with_warning(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch, min_amount=1000, max_amount=500000)
    intent = _create_intent(
        monkeypatch,
        db_session,
        subscriber,
        amount="5000.00",
        reference="ref-topup-low",
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.verify",
        lambda *_args, **_kwargs: SimpleNamespace(
            amount=Decimal("500.00"),
            currency="NGN",
            external_id="ext-topup-low",
            memo_prefix="Paystack",
            metadata={"topup_intent_id": intent["intent_id"]},
        ),
    )
    monkeypatch.setattr("app.services.events.emit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.restore_account_services",
        lambda *_args, **_kwargs: 1,
    )

    result = verify_and_record_topup(
        db_session,
        {"account_id": str(subscriber.id)},
        "ref-topup-low",
        provider="paystack",
    )

    payments = db_session.query(Payment).filter_by(external_id="ext-topup-low").all()

    assert result["already_recorded"] is False
    assert result["amount"] == Decimal("500.00")
    assert result["allocated_total"] == Decimal("0.00")
    assert result["credit_added"] == Decimal("500.00")
    assert len(payments) == 1
    assert result["policy_warnings"]
    assert (
        "Requested ₦5,000.00 but the provider confirmed ₦500.00."
        in result["policy_warnings"]
    )


def test_verify_and_record_topup_records_amount_above_max_with_warning(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch, min_amount=1000, max_amount=500000)
    intent = _create_intent(
        monkeypatch,
        db_session,
        subscriber,
        amount="5000.00",
        reference="ref-topup-high",
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.verify",
        lambda *_args, **_kwargs: SimpleNamespace(
            amount=Decimal("500001.00"),
            currency="NGN",
            external_id="ext-topup-high",
            memo_prefix="Paystack",
            metadata={"topup_intent_id": intent["intent_id"]},
        ),
    )
    monkeypatch.setattr("app.services.events.emit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.restore_account_services",
        lambda *_args, **_kwargs: 1,
    )

    result = verify_and_record_topup(
        db_session,
        {"account_id": str(subscriber.id)},
        "ref-topup-high",
        provider="paystack",
    )

    payments = db_session.query(Payment).filter_by(external_id="ext-topup-high").all()

    assert result["already_recorded"] is False
    assert result["amount"] == Decimal("500001.00")
    assert result["allocated_total"] == Decimal("0.00")
    assert result["credit_added"] == Decimal("500001.00")
    assert len(payments) == 1
    assert result["policy_warnings"]
    assert (
        "Requested ₦5,000.00 but the provider confirmed ₦500,001.00."
        in result["policy_warnings"]
    )


def test_verify_and_record_topup_is_idempotent_and_preserves_summary(
    monkeypatch, db_session, subscriber
):
    invoice = _make_invoice(
        db_session,
        subscriber.id,
        amount="3000.00",
        invoice_number="INV-TOPUP-2",
    )
    _patch_topup_settings(monkeypatch)
    intent = _create_intent(
        monkeypatch,
        db_session,
        subscriber,
        amount="5000.00",
        reference="ref-topup-2",
    )

    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.verify",
        lambda *_args, **_kwargs: SimpleNamespace(
            amount=Decimal("5000.00"),
            currency="NGN",
            external_id="ext-topup-2",
            memo_prefix="Paystack",
            metadata={"topup_intent_id": intent["intent_id"]},
        ),
    )
    monkeypatch.setattr("app.services.events.emit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.restore_account_services",
        lambda *_args, **_kwargs: 1,
    )

    first = verify_and_record_topup(
        db_session,
        {"account_id": str(subscriber.id)},
        "ref-topup-2",
        provider="paystack",
    )
    second = verify_and_record_topup(
        db_session,
        {"account_id": str(subscriber.id)},
        "ref-topup-2",
        provider="paystack",
    )

    payments = db_session.query(Payment).all()

    assert first["already_recorded"] is False
    assert second["already_recorded"] is True
    assert second["allocated_total"] == Decimal("3000.00")
    assert second["credit_added"] == Decimal("2000.00")
    assert len(payments) == 1
    assert second["allocated_to_invoices"][0]["invoice_id"] == str(invoice.id)


def test_verify_and_record_topup_omits_available_balance_when_lookup_fails(
    monkeypatch, db_session, subscriber
):
    _patch_topup_settings(monkeypatch)
    intent = _create_intent(
        monkeypatch,
        db_session,
        subscriber,
        amount="5000.00",
        reference="ref-topup-balance-miss",
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.payment_gateway_adapter.verify",
        lambda *_args, **_kwargs: SimpleNamespace(
            amount=Decimal("5000.00"),
            currency="NGN",
            external_id="ext-topup-balance-miss",
            memo_prefix="Paystack",
            metadata={"topup_intent_id": intent["intent_id"]},
        ),
    )
    monkeypatch.setattr("app.services.events.emit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.restore_account_services",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(
        "app.services.customer_portal_flow_payments.get_available_balance",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("balance lookup failed")
        ),
    )

    result = verify_and_record_topup(
        db_session,
        {"account_id": str(subscriber.id)},
        "ref-topup-balance-miss",
        provider="paystack",
    )

    assert result["already_recorded"] is False
    assert result["available_balance"] is None
