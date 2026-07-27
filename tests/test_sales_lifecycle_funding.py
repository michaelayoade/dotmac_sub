"""Funding-step regressions in the sales-to-service lifecycle.

The funding step is the hinge between the sale and the service: gate 4 of
``docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`` says full funding creates one
pending Subscription and one idempotent ServiceOrder per service line. These
tests pin the ways that used to silently not happen.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.domain_settings import SettingDomain
from app.models.provisioning import ServiceOrder
from app.models.sales import SalesOrderPaymentStatus, SalesOrderStatus
from app.models.subscriber import Subscriber
from app.schemas.sales_order import (
    SalesOrderCreate,
    SalesOrderLineCreate,
    SalesOrderUpdate,
)
from app.services import crm_api
from app.services import sales_lifecycle_reconciliation as reconciler
from app.services import sales_orders as sales_order_service


def _make_subscriber(db) -> Subscriber:
    subscriber = Subscriber(
        first_name="Chidi",
        last_name="Nwosu",
        email=f"chidi-{uuid.uuid4().hex}@example.com",
    )
    db.add(subscriber)
    db.commit()
    db.refresh(subscriber)
    return subscriber


@pytest.fixture()
def billing_calls(monkeypatch):
    """Record the in-process billing calls instead of hitting real services."""
    calls: list[tuple[str, dict]] = []

    def fake_create_subscription(db, **kwargs):
        calls.append(("create_subscription", kwargs))
        return {
            "subscription": SimpleNamespace(id=uuid.uuid4()),
            "invoice": SimpleNamespace(id=uuid.uuid4()),
            "created": True,
        }

    def fake_record_external_payment(db, **kwargs):
        calls.append(("record_external_payment", kwargs))
        return SimpleNamespace(id=uuid.uuid4())

    def fake_create_installation_invoice(db, **kwargs):
        calls.append(("create_installation_invoice", kwargs))
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(crm_api, "create_subscription", fake_create_subscription)
    monkeypatch.setattr(
        crm_api, "record_external_payment", fake_record_external_payment
    )
    monkeypatch.setattr(
        crm_api, "create_installation_invoice", fake_create_installation_invoice
    )
    return calls


@pytest.fixture()
def provisioning_billing(monkeypatch, catalog_offer):
    """Billing fake that persists a real Subscription.

    ``_ensure_provisioning_order_for_sales_line`` re-reads the subscription
    from the database before staging a ServiceOrder, so a ``SimpleNamespace``
    stand-in silently produces no provisioning order. Gate 4 covers both
    halves, so the fixture has to make the row real.
    """
    from app.models.catalog import Subscription, SubscriptionStatus
    from app.services.common import coerce_uuid

    calls: list[tuple[str, dict]] = []

    def fake_create_subscription(db, **kwargs):
        calls.append(("create_subscription", kwargs))
        subscription = Subscription(
            subscriber_id=coerce_uuid(str(kwargs["subscriber_id"])),
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.pending,
        )
        db.add(subscription)
        db.flush()
        return {"subscription": subscription, "invoice": None, "created": True}

    monkeypatch.setattr(crm_api, "create_subscription", fake_create_subscription)
    monkeypatch.setattr(
        crm_api,
        "record_external_payment",
        lambda db, **kw: SimpleNamespace(id=uuid.uuid4()),
    )
    monkeypatch.setattr(
        crm_api,
        "create_installation_invoice",
        lambda db, **kw: SimpleNamespace(id=uuid.uuid4()),
    )
    return calls


def _order_with_service_line(db, *, total="25000.00", offer_id=None):
    subscriber = _make_subscriber(db)
    order = sales_order_service.sales_orders.create(
        db, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    line = sales_order_service.sales_order_lines.create(
        db,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Monthly plan",
            quantity=Decimal("1"),
            unit_price=Decimal(total),
            metadata_={"sub_offer_id": str(offer_id or uuid.uuid4())},
        ),
    )
    db.refresh(order)
    return order, line


# ---------------------------------------------------------------------------
# Finding 1 — deposits go through the SalesOrder financial owner
# ---------------------------------------------------------------------------


def test_deposit_covering_the_total_provisions_the_sale(
    db_session, provisioning_billing, catalog_offer
):
    """A 100% deposit used to leave the order paid but never provisioned.

    ``selfserve_quote_deposit_percent`` accepts 100, and the portal wrote
    payment_status directly, so the funding consequences never fired.
    """
    order, line = _order_with_service_line(db_session, offer_id=catalog_offer.id)

    sales_order_service.record_deposit_receipt(
        db_session,
        sales_order_id=order.id,
        amount=Decimal("25000.00"),
        reference="psk_ref_1",
        provider="paystack",
        actor_id="sales.selfserve",
        ledger_already_recorded=True,
    )

    db_session.refresh(order)
    assert order.payment_status == SalesOrderPaymentStatus.paid.value
    assert order.status == SalesOrderStatus.paid.value
    assert order.deposit_paid is True

    # Gate 4: both halves of the service contract exist.
    assert [name for name, _ in provisioning_billing] == ["create_subscription"]
    db_session.refresh(line)
    assert (line.metadata_ or {}).get("selfcare_subscription_id")
    assert (
        db_session.query(ServiceOrder)
        .filter(ServiceOrder.sales_order_line_id == line.id)
        .count()
        == 1
    )


def test_already_ledgered_deposit_does_not_post_a_second_payment(
    db_session, billing_calls
):
    """Risk #2: one ledger event per deposit, recorded by billing."""
    order, _line = _order_with_service_line(db_session)

    sales_order_service.record_deposit_receipt(
        db_session,
        sales_order_id=order.id,
        amount=Decimal("25000.00"),
        reference="psk_ref_1",
        actor_id="sales.selfserve",
        ledger_already_recorded=True,
    )

    assert "record_external_payment" not in [name for name, _ in billing_calls]


def test_deposit_replay_is_idempotent(db_session, billing_calls):
    order, _line = _order_with_service_line(db_session)
    kwargs = dict(
        sales_order_id=order.id,
        amount=Decimal("10000.00"),
        reference="psk_ref_1",
        actor_id="sales.selfserve",
        ledger_already_recorded=True,
    )

    sales_order_service.record_deposit_receipt(db_session, **kwargs)
    sales_order_service.record_deposit_receipt(db_session, **kwargs)

    db_session.refresh(order)
    # The replay must not double-count the receipt.
    assert order.amount_paid == Decimal("10000.00")
    assert order.payment_status == SalesOrderPaymentStatus.partial.value


def test_two_distinct_deposits_accumulate(db_session, billing_calls):
    """Assigning amount_paid let a second deposit erase the first."""
    order, _line = _order_with_service_line(db_session)
    common = dict(actor_id="sales.selfserve", ledger_already_recorded=True)

    sales_order_service.record_deposit_receipt(
        db_session,
        sales_order_id=order.id,
        amount=Decimal("10000.00"),
        reference="psk_ref_1",
        **common,
    )
    sales_order_service.record_deposit_receipt(
        db_session,
        sales_order_id=order.id,
        amount=Decimal("15000.00"),
        reference="psk_ref_2",
        **common,
    )

    db_session.refresh(order)
    assert order.amount_paid == Decimal("25000.00")
    assert order.payment_status == SalesOrderPaymentStatus.paid.value


def test_same_reference_with_a_different_amount_is_rejected(db_session, billing_calls):
    order, _line = _order_with_service_line(db_session)
    sales_order_service.record_deposit_receipt(
        db_session,
        sales_order_id=order.id,
        amount=Decimal("10000.00"),
        reference="psk_ref_1",
        actor_id="sales.selfserve",
        ledger_already_recorded=True,
    )

    with pytest.raises(sales_order_service.SalesOrderLifecycleError) as exc:
        sales_order_service.record_deposit_receipt(
            db_session,
            sales_order_id=order.id,
            amount=Decimal("99999.00"),
            reference="psk_ref_1",
            actor_id="sales.selfserve",
            ledger_already_recorded=True,
        )

    assert exc.value.code == "deposit_receipt_conflict"
    db_session.refresh(order)
    assert order.amount_paid == Decimal("10000.00")


def test_deposit_receipt_is_recorded_as_evidence(db_session, billing_calls):
    order, _line = _order_with_service_line(db_session)
    sales_order_service.record_deposit_receipt(
        db_session,
        sales_order_id=order.id,
        amount=Decimal("10000.00"),
        reference="psk_ref_1",
        provider="paystack",
        actor_id="sales.selfserve",
        ledger_already_recorded=True,
    )

    db_session.refresh(order)
    receipt = (order.metadata_ or {})["deposit_receipts"]["psk_ref_1"]
    assert receipt["amount"] == "10000.00"
    assert receipt["provider"] == "paystack"
    assert receipt["recorded_by"] == "sales.selfserve"


# ---------------------------------------------------------------------------
# Finding 2 — a waiver is only revoked explicitly
# ---------------------------------------------------------------------------


def test_waiver_survives_a_line_edit(db_session, billing_calls):
    """Every line change recalculates totals, which used to reset the waiver."""
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    order = sales_order_service.record_waiver(
        db_session,
        sales_order_id=order.id,
        actor_id="staff:folake",
        reason="Free relocation for a goodwill case",
    )
    assert order.payment_status == SalesOrderPaymentStatus.waived.value

    sales_order_service.sales_order_lines.create(
        db_session,
        SalesOrderLineCreate(
            sales_order_id=order.id,
            description="Free relocation",
            quantity=Decimal("1"),
            unit_price=Decimal("15000.00"),
        ),
    )

    db_session.refresh(order)
    assert order.payment_status == SalesOrderPaymentStatus.waived.value
    assert order.total == Decimal("15000.00")


def test_waiver_is_still_revocable_explicitly(db_session, billing_calls):
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session,
        SalesOrderCreate(subscriber_id=subscriber.id, total=Decimal("100.00")),
    )
    sales_order_service.record_waiver(
        db_session,
        sales_order_id=order.id,
        actor_id="staff:folake",
        reason="Waived, then reinstated",
    )

    order = sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(payment_status=SalesOrderPaymentStatus.pending),
    )
    assert order.payment_status == SalesOrderPaymentStatus.pending.value


# ---------------------------------------------------------------------------
# Finding 3 — the reconciler notices a funded order that never provisioned
# ---------------------------------------------------------------------------


def test_best_effort_push_swallows_but_strict_push_raises(db_session, monkeypatch):
    """The live path must not break the sale; the repair path must not lie.

    ``_push_sales_order_subscriptions`` deliberately swallows so a billing
    hiccup cannot fail a sale. The reconciler calls the strict entrypoint, so a
    repair that cannot complete surfaces instead of being logged and dropped.
    """
    order, _line = _order_with_service_line(db_session)

    def exploding_create_subscription(db, **kwargs):
        raise RuntimeError("billing is down")

    monkeypatch.setattr(crm_api, "create_subscription", exploding_create_subscription)

    with pytest.raises(RuntimeError):
        sales_order_service.push_sales_order_subscriptions(
            db_session, order, commit=False
        )


def _funded_order_without_subscription(db):
    """The state a swallowed failure leaves behind: paid, but no service.

    Constructed directly rather than by driving a real exception, because the
    best-effort handler rolls the session back and would take the fixture data
    with it.
    """
    order, line = _order_with_service_line(db)
    order.payment_status = SalesOrderPaymentStatus.paid.value
    order.status = SalesOrderStatus.paid.value
    order.amount_paid = order.total
    order.balance_due = Decimal("0.00")
    db.commit()
    db.refresh(order)
    db.refresh(line)
    assert not (line.metadata_ or {}).get("selfcare_subscription_id")
    return order, line


def test_funded_order_without_subscription_is_detected(db_session, billing_calls):
    _order, _line = _funded_order_without_subscription(db_session)

    report = reconciler.reconcile_sales_to_service_lifecycle(db_session, apply=False)
    assert report["funded_orders_missing_subscription"] == 1
    assert report["funded_lines_missing_subscription"] == 1
    assert report["subscriptions_repaired"] == 0


def test_reconciler_repairs_the_missing_subscription(db_session, billing_calls):
    _order, line = _funded_order_without_subscription(db_session)

    report = reconciler.reconcile_sales_to_service_lifecycle(db_session, apply=True)
    assert report["subscriptions_repaired"] == 1

    db_session.refresh(line)
    assert (line.metadata_ or {}).get("selfcare_subscription_id")

    # And the repair is idempotent — a second sweep finds nothing left to do.
    again = reconciler.reconcile_sales_to_service_lifecycle(db_session, apply=True)
    assert again["funded_orders_missing_subscription"] == 0
    assert again["subscriptions_repaired"] == 0


def test_unresolvable_offer_is_reported_not_silently_skipped(
    db_session, billing_calls, monkeypatch
):
    """A line whose offer no longer resolves must not read as a clean sweep."""
    _order, _line = _funded_order_without_subscription(db_session)

    def missing_offer(db, **kwargs):
        raise LookupError("offer not found")

    monkeypatch.setattr(crm_api, "create_subscription", missing_offer)

    report = reconciler.reconcile_sales_to_service_lifecycle(db_session, apply=True)
    assert report["subscriptions_repaired"] == 0
    assert report["unresolvable_offer_lines"] == 1


def test_clean_lifecycle_reports_no_funding_drift(db_session, billing_calls):
    order, _line = _order_with_service_line(db_session)
    sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(
            payment_status=SalesOrderPaymentStatus.paid, paid_at=datetime.now(UTC)
        ),
    )

    report = reconciler.reconcile_sales_to_service_lifecycle(db_session, apply=False)
    assert report["funded_orders_missing_subscription"] == 0


# ---------------------------------------------------------------------------
# Finding 4 — the reconciler is actually scheduled, and detect-only by default
# ---------------------------------------------------------------------------


def test_reconcile_toggle_is_a_registered_scheduler_control():
    from app.services.settings_spec import SCHEDULER_BOOLEAN_SETTING_KEYS

    assert (
        SettingDomain.projects,
        "sales_lifecycle_reconcile_enabled",
    ) in SCHEDULER_BOOLEAN_SETTING_KEYS


def test_repair_is_opt_in(db_session):
    """Repair creates subscriptions and invoices, so it must not default on."""
    from app.tasks.sales_lifecycle import _apply_enabled

    assert _apply_enabled(db_session) is False


def test_scheduler_registers_the_reconciler():
    from pathlib import Path

    source = Path("app/services/scheduler_config.py").read_text()
    assert "app.tasks.sales_lifecycle.reconcile_sales_to_service_lifecycle" in source


# ---------------------------------------------------------------------------
# Finding 5 — a funded order cannot be hidden from the reconciler
# ---------------------------------------------------------------------------


def test_paid_order_cannot_be_deactivated(db_session, billing_calls):
    order, _line = _order_with_service_line(db_session)
    sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(
            payment_status=SalesOrderPaymentStatus.paid, paid_at=datetime.now(UTC)
        ),
    )

    with pytest.raises(HTTPException) as exc:
        sales_order_service.sales_orders.delete(db_session, str(order.id))

    assert exc.value.status_code == 409
    db_session.refresh(order)
    assert order.is_active is True


def test_order_with_implementation_scope_cannot_be_deactivated(
    db_session, billing_calls
):
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    db_session.refresh(order)
    assert order.project is not None  # created by the fulfilment coordinator

    with pytest.raises(HTTPException) as exc:
        sales_order_service.sales_orders.delete(db_session, str(order.id))
    assert exc.value.status_code == 409


def test_deactivation_is_idempotent_for_an_already_inactive_order(
    db_session, billing_calls, monkeypatch
):
    subscriber = _make_subscriber(db_session)
    order = sales_order_service.sales_orders.create(
        db_session, SalesOrderCreate(subscriber_id=subscriber.id)
    )
    order.is_active = False
    db_session.commit()

    sales_order_service.sales_orders.delete(db_session, str(order.id))
    db_session.refresh(order)
    assert order.is_active is False


# ---------------------------------------------------------------------------
# Finding 6 — a staff-asserted settlement is attributable
# ---------------------------------------------------------------------------


def test_inferred_settlement_is_marked_and_attributed(db_session, billing_calls):
    """Flipping an order to paid back-fills the amount and posts to the ledger.

    That is intended, but the resulting money must not look like a receipt.
    """
    order, _line = _order_with_service_line(db_session)

    order = sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(
            payment_status=SalesOrderPaymentStatus.paid, paid_at=datetime.now(UTC)
        ),
        actor_id="staff:folake",
    )

    metadata = order.metadata_ or {}
    assert metadata["payment_amount_source"] == sales_order_service.AMOUNT_INFERRED
    assert metadata["payment_confirmed_by"] == "staff:folake"

    payment = next(
        kw for name, kw in billing_calls if name == "record_external_payment"
    )
    assert "staff:folake" in payment["memo"]
    assert "inferred from order total" in payment["memo"]


def test_observed_amount_is_not_marked_inferred(db_session, billing_calls):
    order, _line = _order_with_service_line(db_session)

    order = sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(amount_paid=Decimal("25000.00")),
        actor_id="staff:folake",
    )

    metadata = order.metadata_ or {}
    assert metadata["payment_amount_source"] == sales_order_service.AMOUNT_OBSERVED
    payment = next(
        kw for name, kw in billing_calls if name == "record_external_payment"
    )
    assert "inferred" not in payment["memo"]


def test_unattributed_settlement_is_recorded_as_such(db_session, billing_calls):
    """No actor is still recorded, so the gap is visible rather than blank."""
    order, _line = _order_with_service_line(db_session)

    order = sales_order_service.sales_orders.update(
        db_session,
        str(order.id),
        SalesOrderUpdate(
            payment_status=SalesOrderPaymentStatus.paid, paid_at=datetime.now(UTC)
        ),
    )

    assert (order.metadata_ or {})["payment_confirmed_by"] == "unattributed"
