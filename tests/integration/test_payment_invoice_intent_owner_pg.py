"""Migrated PostgreSQL atomicity/concurrency and real app_user getter evidence.

Root commands use app_user and the actual application Session tenant hook.
Setup alone uses the migrated database fixture principal; no test-only grants.
"""

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money
from dotmac_payments import (
    GetPaymentIntentV1,
    OpenPaymentIntent,
    PaymentPurpose,
    get_payment_intent_v1,
    open_payment_intent,
)
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus, TopupIntent
from app.models.event_store import EventStore
from app.models.subscriber import Reseller, Subscriber
from app.services import gateway_topup_intents as svc
from app.services import session_hooks, topup_intents
from app.services.events import dispatcher
from app.services.owner_commands import CommandContext
from tests.integration_platform_helpers import enable_payment_provider


@dataclass(frozen=True)
class InvoiceCase:
    engine: Engine
    setup_engine: Engine
    command: svc.CreateCustomerGatewayTopupIntentCommand


def context() -> CommandContext:
    return CommandContext.system(
        actor="pytest:invoice-module",
        scope=svc.CREATE_CUSTOMER_SCOPE,
        reason="Migrated invoice module owner proof",
    )


@pytest.fixture
def invoice_case(
    cloned_database: Callable[[str], URL], monkeypatch: pytest.MonkeyPatch
) -> Iterator[InvoiceCase]:
    engine = sa.create_engine(cloned_database("heads"))
    # Persist the real event/outbox row; delivery belongs to a separate owner.
    monkeypatch.setattr(dispatcher, "run_after_commit", lambda _db, _callback: None)
    session_hooks.install_session_hooks()
    try:
        with Session(engine) as setup:
            reseller_id = setup.scalar(
                sa.select(Reseller.id).where(Reseller.is_house.is_(True))
            )
            assert reseller_id is not None
            account = Subscriber(
                first_name="Invoice",
                last_name="Canary",
                email=f"invoice-{uuid4()}@example.test",
                reseller_id=reseller_id,
            )
            setup.add(account)
            setup.flush()
            invoice = Invoice(
                account_id=account.id,
                invoice_number=f"INV-{uuid4()}",
                currency="NGN",
                subtotal=Decimal("3750.00"),
                total=Decimal("3750.00"),
                balance_due=Decimal("3750.00"),
                status=InvoiceStatus.issued,
            )
            setup.add(invoice)
            setup.commit()
            binding = enable_payment_provider(setup, "paystack")["payments.intent.v1"]
            command = svc.CreateCustomerGatewayTopupIntentCommand(
                flow=svc.CustomerGatewayTopupFlow.invoice_payment,
                account_id=account.id,
                invoice_id=invoice.id,
                reference=f"invoice-module-{uuid4()}",
                provider_type="paystack",
                provider_id=None,
                created_by="pytest",
                capability_binding_id=binding.id,
            )
            setup.commit()
        runtime = sa.create_engine(engine.url)

        @sa.event.listens_for(runtime, "connect")
        def runtime_role(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("SET ROLE app_user")
            cursor.close()
            dbapi_connection.commit()

        try:
            yield InvoiceCase(runtime, engine, command)
        finally:
            runtime.dispose()
    finally:
        engine.dispose()


def create(
    case: InvoiceCase,
    command: svc.CreateCustomerGatewayTopupIntentCommand | None = None,
) -> svc.GatewayTopupIntentResult:
    with Session(case.engine) as db:
        return svc.create_customer_gateway_topup_intent(
            db, command or case.command, context=context()
        )


def event_count(db: Session, reference: str) -> int:
    return (
        db.scalar(
            sa.select(sa.func.count())
            .select_from(EventStore)
            .where(
                EventStore.event_type == "topup_intent.gateway_created",
                EventStore.payload["reference"].astext == reference,
            )
        )
        or 0
    )


def test_real_owner_preserves_binding_replay_and_app_hook_getter_rls(
    invoice_case: InvoiceCase,
) -> None:
    case = invoice_case
    first = create(case)
    with Session(case.engine) as db:
        assert db.scalar(sa.text("SELECT current_user")) == "app_user"
        assert db.scalar(
            sa.text("SELECT current_setting('app.current_tenant')")
        ) == str(svc.OPERATOR_TENANT_ID)
        local = db.get(TopupIntent, first.intent_id)
        assert local is not None
        bound = get_payment_intent_v1(
            db,
            scope=TenantScope(svc.OPERATOR_TENANT_ID),
            query=GetPaymentIntentV1(first.reference),
        )
        assert bound is not None
        assert local.module_intent_id == bound.intent_id == local.id
        assert local.module_intent_tenant_id == bound.scope.tenant_id
        assert local.created_at == bound.opened_at
        assert local.expires_at == bound.expires_at
        local.status = "expired"
        local.metadata_ = {**local.metadata_, "reconciliation_observed": "preserved"}
        db.commit()
    with Session(case.setup_engine) as setup:
        invoice = setup.get(Invoice, case.command.invoice_id)
        assert invoice is not None
        invoice.balance_due = Decimal("1.00")
        invoice.status = InvoiceStatus.paid
        setup.commit()
    second = create(case)
    assert second.replayed
    assert second.intent_id == first.intent_id
    assert second.requested_amount == first.requested_amount
    assert second.expires_at == first.expires_at
    with Session(case.engine) as db:
        local = db.get(TopupIntent, first.intent_id)
        assert local.status == "expired"
        assert local.metadata_["reconciliation_observed"] == "preserved"
        assert event_count(db, first.reference) == 1

    runtime = sa.create_engine(case.engine.url)

    @sa.event.listens_for(runtime, "connect")
    def runtime_role(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("SET ROLE app_user")
        cursor.close()
        dbapi_connection.commit()

    try:
        with Session(runtime) as db:
            observed = get_payment_intent_v1(
                db,
                scope=TenantScope(svc.OPERATOR_TENANT_ID),
                query=GetPaymentIntentV1(first.reference),
            )
            assert observed is not None
            assert db.scalar(sa.text("SELECT current_user")) == "app_user"
            assert db.scalar(
                sa.text("SELECT current_setting('app.current_tenant')")
            ) == str(svc.OPERATOR_TENANT_ID)
            assert (
                get_payment_intent_v1(
                    db,
                    scope=TenantScope(uuid4()),
                    query=GetPaymentIntentV1(first.reference),
                )
                is None
            )
    finally:
        runtime.dispose()


def test_real_owner_refuses_module_only_and_local_only_reconstruction(
    invoice_case: InvoiceCase,
) -> None:
    case = invoice_case
    module_only = replace(case.command, reference=f"module-only-{uuid4()}")
    with Session(case.engine) as db:
        opened_at = datetime.now(UTC)
        open_payment_intent(
            db,
            scope=TenantScope(svc.OPERATOR_TENANT_ID),
            command=OpenPaymentIntent(
                reference=module_only.reference,
                payer_reference=str(module_only.account_id),
                target_reference=str(module_only.invoice_id),
                purpose=PaymentPurpose.INVOICE_SETTLEMENT,
                requested=Money(Decimal("3750.00"), Currency("NGN", 2)),
                provider_type="paystack",
                channel="customer_selfcare",
                opened_at=opened_at,
                expires_at=opened_at + timedelta(minutes=30),
            ),
        )
        db.commit()
    with pytest.raises(svc.GatewayTopupIntentError) as refusal:
        create(case, module_only)
    assert refusal.value.code.endswith("invoice_binding_incomplete")
    # A legacy reference from a different workflow is still local-only; it
    # must never be silently reclassified or reconstructed as an invoice.
    local_only = replace(case.command, reference=f"local-only-{uuid4()}")
    with Session(case.engine) as db:
        db.add(
            TopupIntent(
                account_id=local_only.account_id,
                reference=local_only.reference,
                provider_type="paystack",
                requested_amount=Decimal("1.00"),
                currency="NGN",
                metadata_={"payment_flow": "legacy_other_flow"},
            )
        )
        db.commit()
    with pytest.raises(svc.GatewayTopupIntentError) as refusal:
        create(case, local_only)
    assert refusal.value.code.endswith("invoice_binding_incomplete")


def test_real_module_projection_and_event_roll_back_as_one_root(
    invoice_case: InvoiceCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = invoice_case
    original = topup_intents.stage_gateway_topup_intent_created_event

    def fail_after_event(
        db: Session, *, intent: TopupIntent, context: CommandContext
    ) -> None:
        original(db, intent=intent, context=context)
        raise RuntimeError("after durable event staging")

    monkeypatch.setattr(
        topup_intents, "stage_gateway_topup_intent_created_event", fail_after_event
    )
    with pytest.raises(RuntimeError, match="after durable event staging"):
        create(case)
    with Session(case.engine) as db:
        assert (
            get_payment_intent_v1(
                db,
                scope=TenantScope(svc.OPERATOR_TENANT_ID),
                query=GetPaymentIntentV1(case.command.reference),
            )
            is None
        )
        assert (
            db.scalar(
                sa.select(TopupIntent.id).where(
                    TopupIntent.reference == case.command.reference
                )
            )
            is None
        )
        assert event_count(db, case.command.reference) == 0


def test_real_same_reference_race_creates_one_binding_projection_and_event(
    invoice_case: InvoiceCase,
) -> None:
    case = invoice_case
    barrier = Barrier(2)

    def attempt(_index: int) -> svc.GatewayTopupIntentResult:
        barrier.wait(timeout=10)
        return create(case)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(attempt, range(2)))
    assert len({result.intent_id for result in results}) == 1
    assert sorted(result.replayed for result in results) == [False, True]
    with Session(case.engine) as db:
        assert (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(TopupIntent)
                .where(TopupIntent.reference == case.command.reference)
            )
            == 1
        )
        assert event_count(db, case.command.reference) == 1


def test_cross_account_reference_race_cannot_leave_an_orphan_binding(
    invoice_case: InvoiceCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = invoice_case
    with Session(case.setup_engine) as setup:
        reseller_id = setup.scalar(
            sa.select(Reseller.id).where(Reseller.is_house.is_(True))
        )
        other = Subscriber(
            first_name="Other",
            last_name="Payer",
            email=f"other-{uuid4()}@example.test",
            reseller_id=reseller_id,
        )
        setup.add(other)
        setup.flush()
        invoice = Invoice(
            account_id=other.id,
            invoice_number=f"INV-OTHER-{uuid4()}",
            currency="NGN",
            subtotal=Decimal("3750.00"),
            total=Decimal("3750.00"),
            balance_due=Decimal("3750.00"),
            status=InvoiceStatus.issued,
        )
        setup.add(invoice)
        setup.commit()
        other_command = replace(
            case.command, account_id=other.id, invoice_id=invoice.id
        )
    # Both roots have read module/local absence before entering the public
    # opener. Block its first flush so BOTH module SELECTs necessarily see
    # absence, forcing the real unique-index conflict rather than a serial
    # preflight refusal. Accounts/invoices are independently locked.
    real_open = svc.open_payment_intent
    flush_barrier = Barrier(2)

    def racing_open(
        db: Session, *, scope: TenantScope, command: OpenPaymentIntent
    ) -> None:
        def before_first_flush(_db: Session, _context, _instances) -> None:
            flush_barrier.wait(timeout=10)

        sa.event.listen(db, "before_flush", before_first_flush, once=True)
        try:
            real_open(db, scope=scope, command=command)
        finally:
            sa.event.remove(db, "before_flush", before_first_flush)

    monkeypatch.setattr(svc, "open_payment_intent", racing_open)
    barrier = Barrier(2)

    def attempt(
        command: svc.CreateCustomerGatewayTopupIntentCommand,
    ) -> svc.GatewayTopupIntentResult | svc.GatewayTopupIntentError:
        barrier.wait(timeout=10)
        try:
            return create(case, command)
        except svc.GatewayTopupIntentError as refusal:
            return refusal

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(attempt, (case.command, other_command)))
    successes = [
        item for item in outcomes if isinstance(item, svc.GatewayTopupIntentResult)
    ]
    refusals = [
        item for item in outcomes if isinstance(item, svc.GatewayTopupIntentError)
    ]
    assert len(successes) == len(refusals) == 1
    from sqlalchemy.exc import IntegrityError

    assert refusals[0].code.endswith("intent_conflict")
    assert isinstance(refusals[0].__cause__, IntegrityError)
    with Session(case.engine) as db:
        bound = get_payment_intent_v1(
            db,
            scope=TenantScope(svc.OPERATOR_TENANT_ID),
            query=GetPaymentIntentV1(case.command.reference),
        )
        assert bound is not None
        local = db.get(TopupIntent, successes[0].intent_id)
        assert local is not None and local.module_intent_id == bound.intent_id
        assert str(local.account_id) == bound.payer_reference
        assert event_count(db, case.command.reference) == 1
        assert (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(TopupIntent)
                .where(TopupIntent.reference == case.command.reference)
            )
            == 1
        )
        loser_account = (
            other_command.account_id
            if successes[0].account_id == case.command.account_id
            else case.command.account_id
        )
        assert (
            db.scalar(
                sa.select(sa.func.count())
                .select_from(TopupIntent)
                .where(
                    TopupIntent.reference == case.command.reference,
                    TopupIntent.account_id == loser_account,
                )
            )
            == 0
        )
        assert (
            db.scalar(
                sa.text(
                    "SELECT count(*) FROM mod_payments.payment_intents WHERE tenant_id = :tenant AND reference = :reference"
                ),
                {"tenant": svc.OPERATOR_TENANT_ID, "reference": case.command.reference},
            )
            == 1
        )
