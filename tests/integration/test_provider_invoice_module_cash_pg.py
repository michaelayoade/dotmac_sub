"""Hosted foundation canaries; no cash acceptance or final handoff is claimed.

Real Alembic heads and an actual app_user login are mandatory. The shared
pristine hosted fixture owns temporary test authentication and cleanup; no
financial privilege/role repair or fabricated approval is introduced.
Positive confirmation/posting/concurrency tests await the qualified physical
merchant namespace and published exact Billing assembly.
"""

from collections.abc import Callable, Iterator
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from dotmac_billing import create_billing_account
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money
from dotmac_payments import OpenPaymentIntent, PaymentPurpose, open_payment_intent
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from alembic import command as alembic_command
from app.commercial_module_prereqs import require_billing_runtime_login
from app.models.billing import (
    Invoice,
    ModuleBillingAccountBinding,
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderEventSource,
    PaymentProviderType,
    PaymentStatus,
    TopupIntent,
)
from app.models.subscriber import Subscriber
from app.services import session_hooks
from app.services.events import dispatcher
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import CommandContext
from app.services.payment_provider_events import (
    ADMINISTRATIVE_INGEST_SCOPE,
    PaymentProviderEventCommand,
    PaymentProviderEventError,
    PaymentProviderEventResult,
    PaymentProviderEvents,
)
from app.services.provider_payment_settlements import (
    ModuleInvoiceBillingAccountQuery,
    ModuleInvoiceCashSourceError,
    ModuleInvoiceCashSourceQuery,
    get_module_invoice_billing_account,
    get_module_invoice_cash_source,
)
from tests.integration import (
    test_billing_runtime_login_postgres as hosted_runtime_login,
)
from tests.integration.test_billing_runtime_login_postgres import (
    RuntimeConnections,
)

# Explicit fixture re-export: shared owner retains authentication/cleanup custody.
runtime_connections = hosted_runtime_login.runtime_connections


@dataclass(frozen=True, slots=True)
class SourceCase:
    admin: Engine
    runtime: Engine
    query: ModuleInvoiceCashSourceQuery
    account_id: UUID
    provider_id: UUID
    billing_account_id: UUID
    occurred_at: datetime
    observed_at: datetime


@pytest.fixture
def source_case(
    runtime_connections: RuntimeConnections, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SourceCase]:
    session_hooks.install_session_hooks()
    monkeypatch.setattr(dispatcher, "run_after_commit", lambda _db, _callback: None)
    # Reuse the existing isolated-host/maintenance-lock/pristine-posture fixture.
    # It owns temporary authentication and independent original-NULL cleanup.
    with runtime_connections.runtime.transaction():
        require_billing_runtime_login(runtime_connections.runtime)
    runtime_connections.admin.autocommit = False
    admin = create_engine(
        "postgresql+psycopg://",
        creator=lambda: runtime_connections.admin,
        poolclass=StaticPool,
    )
    runtime = create_engine(
        "postgresql+psycopg://",
        creator=lambda: runtime_connections.runtime,
        poolclass=StaticPool,
    )
    try:
        with runtime.begin() as connection:
            assert connection.execute(
                text("SELECT session_user, current_user")
            ).one() == ("app_user", "app_user")
            assert connection.scalar(
                text(
                    "SELECT NOT (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb) "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        scope = TenantScope(OPERATOR_TENANT_ID)
        opened = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
        occurred = opened + timedelta(minutes=1)
        # Late receipt must retain the original successful capture time.
        received = opened + timedelta(days=2)
        reference = f"cash-source-{uuid4()}"
        with Session(admin) as db:
            account = Subscriber(
                first_name="Cash", last_name="Canary", email=f"{uuid4()}@example.test"
            )
            provider = PaymentProvider(
                name=f"cash-source-{uuid4()}",
                provider_type=PaymentProviderType.paystack,
            )
            db.add_all([account, provider])
            db.flush()
            invoice = Invoice(account_id=account.id, currency="NGN")
            db.add(invoice)
            db.flush()
            module = open_payment_intent(
                db,
                scope=scope,
                command=OpenPaymentIntent(
                    reference=reference,
                    payer_reference=str(account.id),
                    target_reference=str(invoice.id),
                    purpose=PaymentPurpose.INVOICE_SETTLEMENT,
                    requested=Money(Decimal("100.00"), Currency("NGN", 2)),
                    provider_type="paystack",
                    channel="customer_selfcare",
                    opened_at=opened,
                    expires_at=opened + timedelta(minutes=30),
                ),
            )
            local = TopupIntent(
                id=module.id,
                module_intent_id=module.id,
                module_intent_tenant_id=OPERATOR_TENANT_ID,
                account_id=account.id,
                invoice_id=invoice.id,
                provider_id=provider.id,
                reference=reference,
                provider_type="paystack",
                channel="customer_selfcare",
                currency="NGN",
                requested_amount=Decimal("100.00"),
                created_at=opened,
                expires_at=opened + timedelta(minutes=30),
            )
            billing = create_billing_account(
                db,
                scope=scope,
                external_account_ref=str(account.id),
                currency="NGN",
                minor_units=2,
            )
            binding = ModuleBillingAccountBinding(
                tenant_id=OPERATOR_TENANT_ID,
                account_id=account.id,
                currency="NGN",
                billing_account_id=billing.id,
            )
            # Privileged fixture seeds normalized provider facts, not a claimed
            # namespace or acceptance receipt. Production admission is separate.
            event = PaymentProviderEvent(
                provider_id=provider.id,
                event_type="charge.success",
                external_id=f"transaction-{uuid4()}",
                source=PaymentProviderEventSource.verified_webhook,
                observation_digest="a" * 64,
                observed_payment_status=PaymentStatus.succeeded,
                amount=Decimal("102.00"),
                provider_fee=Decimal("2.00"),
                net_amount=Decimal("100.00"),
                currency="NGN",
                provider_reference=reference,
                occurred_at=occurred,
                received_at=received,
            )
            db.add_all([local, binding, event])
            db.flush()
            result = SourceCase(
                admin,
                runtime,
                ModuleInvoiceCashSourceQuery(event.id, reference),
                account.id,
                provider.id,
                billing.id,
                occurred,
                received,
            )
            db.commit()
        yield result
    finally:
        runtime.dispose()
        admin.dispose()


def test_late_receipt_reads_original_capture_and_never_posts(
    source_case: SourceCase,
) -> None:
    with Session(source_case.runtime) as db, db.begin():
        source = get_module_invoice_cash_source(db, source_case.query)
        assert source.occurred_at == source_case.occurred_at
        assert source.observed_at == source_case.observed_at
        assert source.net_amount == Decimal("100.00")
        assert (
            db.scalar(
                text("SELECT count(*) FROM public.module_invoice_cash_correlations")
            )
            == 0
        )
        assert (
            db.scalar(
                text(
                    "SELECT count(*) FROM mod_payments.payment_confirmations WHERE intent_id=:id"
                ),
                {"id": source.module_intent_id},
            )
            == 0
        )
        assert (
            db.scalar(
                text(
                    "SELECT count(*) FROM mod_billing.confirmed_settlements WHERE billing_account_id=:id"
                ),
                {"id": source_case.billing_account_id},
            )
            == 0
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "occurred_at = NULL",
        "occurred_at = received_at",
        "currency = 'USD'",
        "provider_reference = 'other-checkout'",
        "net_amount = 99",
        "source = 'administrative_ingest'",
        "financial_effect = 'refund_confirmed'",
    ],
)
def test_missing_or_changed_source_facts_refuse(
    source_case: SourceCase, mutation: str
) -> None:
    with source_case.admin.begin() as connection:
        connection.execute(
            text(f"UPDATE public.payment_provider_events SET {mutation} WHERE id=:id"),  # noqa: S608 - closed parametrized SQL
            {"id": source_case.query.observation_id},
        )
    with (
        Session(source_case.runtime) as db,
        db.begin(),
        pytest.raises(ModuleInvoiceCashSourceError),
    ):
        get_module_invoice_cash_source(db, source_case.query)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE public.module_billing_account_bindings SET currency='USD'",
        "DELETE FROM public.module_billing_account_bindings",
        "TRUNCATE public.module_billing_account_bindings",
    ],
)
def test_account_binding_is_immutable_even_for_setup_owner(
    source_case: SourceCase, statement: str
) -> None:
    with (
        pytest.raises(DBAPIError, match="append-only"),
        source_case.admin.begin() as connection,
    ):
        connection.execute(text(statement))


def test_online_binding_writer_is_not_granted(source_case: SourceCase) -> None:
    with source_case.runtime.begin() as connection:
        assert not connection.scalar(
            text(
                "SELECT has_table_privilege(current_user, 'public.module_billing_account_bindings', 'INSERT')"
            )
        )
        assert not connection.scalar(
            text(
                "SELECT has_table_privilege(current_user, 'public.module_invoice_cash_correlations', 'INSERT')"
            )
        )
    with Session(source_case.admin) as db:
        assert (
            db.scalar(
                select(ModuleBillingAccountBinding.billing_account_id).where(
                    ModuleBillingAccountBinding.account_id == source_case.account_id
                )
            )
            == source_case.billing_account_id
        )


def _ingest_informational(
    case: SourceCase, command: PaymentProviderEventCommand
) -> PaymentProviderEventResult:
    with Session(case.admin) as db:
        return PaymentProviderEvents.ingest(
            db,
            command,
            context=CommandContext.system(
                actor="pytest:cash-source-time",
                scope=ADMINISTRATIVE_INGEST_SCOPE,
                reason="Typed informational timestamp replay canary; no cash authority",
            ),
        )


def test_typed_occurrence_persists_and_exact_replay_keeps_first_receipt(
    source_case: SourceCase,
) -> None:
    command = PaymentProviderEventCommand(
        provider_id=source_case.provider_id,
        event_type="provider.informational",
        external_id=f"time-canary-{uuid4()}",
        occurred_at=source_case.occurred_at,
    )
    first = _ingest_informational(source_case, command)
    replay = _ingest_informational(source_case, command)
    assert replay.id == first.id
    assert replay.replayed
    assert replay.received_at == first.received_at
    with Session(source_case.admin) as db:
        persisted = db.get(PaymentProviderEvent, first.id)
        assert persisted is not None
        assert persisted.occurred_at == command.occurred_at
        assert persisted.payment_id is None
        assert persisted.amount is None
        assert persisted.received_at == first.received_at
    with pytest.raises(PaymentProviderEventError, match="different evidence"):
        _ingest_informational(
            source_case,
            replace(
                command, occurred_at=source_case.occurred_at + timedelta(seconds=1)
            ),
        )
    with Session(source_case.admin) as db:
        assert (
            db.scalar(
                select(PaymentProviderEvent.received_at).where(
                    PaymentProviderEvent.id == first.id
                )
            )
            == first.received_at
        )
        assert (
            db.scalar(
                select(PaymentProviderEvent.occurred_at).where(
                    PaymentProviderEvent.id == first.id
                )
            )
            == command.occurred_at
        )


@pytest.mark.parametrize("legacy_digest_absent", [False, True])
def test_null_occurrence_history_cannot_be_replay_enriched(
    source_case: SourceCase, legacy_digest_absent: bool
) -> None:
    command = PaymentProviderEventCommand(
        provider_id=source_case.provider_id,
        event_type="provider.informational",
        external_id=f"null-time-canary-{uuid4()}",
    )
    first = _ingest_informational(source_case, command)
    if legacy_digest_absent:
        # Simulate genuine pre-digest history without granting financial facts.
        with Session(source_case.admin) as db, db.begin():
            event = db.get(PaymentProviderEvent, first.id)
            assert event is not None
            event.observation_digest = None
    with pytest.raises(PaymentProviderEventError, match="different evidence"):
        _ingest_informational(
            source_case, replace(command, occurred_at=source_case.occurred_at)
        )
    with Session(source_case.admin) as db:
        row = db.get(PaymentProviderEvent, first.id)
        assert row is not None
        assert row.occurred_at is None
        assert row.received_at == first.received_at
        assert row.payment_id is None


def test_typed_naive_occurrence_refuses_before_insert(source_case: SourceCase) -> None:
    external_id = f"naive-time-canary-{uuid4()}"
    with pytest.raises(PaymentProviderEventError, match="timezone-aware"):
        _ingest_informational(
            source_case,
            PaymentProviderEventCommand(
                provider_id=source_case.provider_id,
                event_type="provider.informational",
                external_id=external_id,
                occurred_at=source_case.occurred_at.replace(tzinfo=None),
            ),
        )
    with Session(source_case.admin) as db:
        assert (
            db.scalar(
                select(PaymentProviderEvent.id).where(
                    PaymentProviderEvent.external_id == external_id
                )
            )
            is None
        )


@pytest.mark.parametrize(
    "native_ddl", [False, True], ids=("known_empty_adoption", "prospective_absent")
)
def test_692_absence_or_known_empty_adoption_branch(
    cloned_database: Callable[[str], URL],
    native_ddl: bool,
) -> None:
    engine = create_engine(cloned_database("691_payments_intent_correlation"))
    try:
        if native_ddl:
            # The squashed base declares today's unused prospective ORM shape.
            # Remove only these future declarations to exercise real CREATE/ADD
            # on the same migrated predecessor, without stamp/create_all.
            with engine.begin() as connection:
                connection.execute(
                    text("DROP TABLE public.module_invoice_cash_correlations")
                )
                connection.execute(
                    text("DROP TABLE public.module_billing_account_bindings")
                )
                connection.execute(
                    text(
                        "ALTER TABLE public.payment_provider_events DROP COLUMN occurred_at"
                    )
                )
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        alembic_command.upgrade(config, "692_module_invoice_cash")
        with engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM public.alembic_version WHERE version_num='692_module_invoice_cash')"
                )
            )
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM public.module_invoice_cash_correlations")
                )
                == 0
            )
            assert (
                connection.scalar(
                    text("""
                SELECT count(*) FROM pg_catalog.pg_class c
                WHERE c.oid IN ('public.module_billing_account_bindings'::regclass,
                    'public.module_invoice_cash_correlations'::regclass)
                AND c.relkind = 'r' AND c.relrowsecurity AND c.relforcerowsecurity
                AND (SELECT count(*) FROM pg_catalog.pg_policy p WHERE p.polrelid=c.oid) = 1
            """)
                )
                == 2
            )
            assert (
                connection.scalar(
                    text("""
                SELECT count(*) FROM pg_catalog.pg_constraint c
                WHERE c.conrelid IN ('public.module_billing_account_bindings'::regclass,
                    'public.module_invoice_cash_correlations'::regclass)
                AND c.contype='f' AND c.convalidated AND NOT c.condeferrable
                AND (SELECT count(*) FROM pg_catalog.pg_trigger t
                    WHERE t.tgconstraint=c.oid AND t.tgisinternal AND t.tgenabled='O')=4
            """)
                )
                == 8
            )
            assert (
                connection.scalar(
                    text("""
                SELECT count(*) FROM pg_catalog.pg_constraint
                WHERE conname IN ('fk_module_billing_binding_account',
                    'fk_module_invoice_cash_account','fk_module_invoice_cash_settlement',
                    'fk_module_invoice_cash_intent','fk_module_invoice_cash_confirmation')
                AND conrelid IN ('public.module_billing_account_bindings'::regclass,
                    'public.module_invoice_cash_correlations'::regclass)
                AND contype='f' AND convalidated
            """)
                )
                == 5
            )
            assert (
                connection.scalar(
                    text("""
                SELECT count(*) FROM pg_catalog.pg_trigger t
                JOIN pg_catalog.pg_proc f ON f.oid=t.tgfoid
                WHERE t.tgrelid IN ('public.module_billing_account_bindings'::regclass,
                    'public.module_invoice_cash_correlations'::regclass)
                AND NOT t.tgisinternal AND t.tgenabled='O' AND t.tgtype IN (27,34)
                AND f.oid='public.guard_module_invoice_cash_append_only()'::regprocedure
                AND NOT f.prosecdef
            """)
                )
                == 4
            )

    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "mutation",
    [
        "ALTER TABLE public.module_billing_account_bindings ADD COLUMN unexpected integer",
        "ALTER TABLE public.module_invoice_cash_correlations ALTER COLUMN source_system SET DEFAULT 'unexpected'",
        "ALTER TABLE public.payment_provider_events ALTER COLUMN occurred_at SET DEFAULT now()",
        "CREATE INDEX unexpected_cash_index ON public.module_invoice_cash_correlations (observed_at)",
        "CREATE INDEX unexpected_occurrence_index ON public.payment_provider_events (occurred_at)",
        "ALTER TABLE public.module_billing_account_bindings DISABLE TRIGGER ALL",
        "ALTER TABLE public.module_invoice_cash_correlations DISABLE TRIGGER ALL",
        "ALTER TABLE public.subscribers DISABLE TRIGGER ALL",
    ],
)
def test_real_predecessor_upgrade_refuses_unexpected_prospective_shape(
    cloned_database: Callable[[str], URL], mutation: str
) -> None:
    engine = create_engine(cloned_database("691_payments_intent_correlation"))
    try:
        with engine.begin() as connection:
            connection.execute(text(mutation))
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        with pytest.raises(RuntimeError, match="Unexpected prospective"):
            alembic_command.upgrade(config, "692_module_invoice_cash")
        with engine.connect() as connection:
            assert not connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM public.alembic_version WHERE version_num='692_module_invoice_cash')"
                )
            )
    finally:
        engine.dispose()


def test_candidate_public_billing_account_consumer_reads_current_frozen_identity(
    source_case: SourceCase,
) -> None:
    """Requires the actually published/pinned public query; a1 must fail this gate."""
    with Session(source_case.runtime) as db, db.begin():
        result = get_module_invoice_billing_account(
            db,
            ModuleInvoiceBillingAccountQuery(
                observation_id=source_case.query.observation_id,
                checkout_reference=source_case.query.checkout_reference,
            ),
        )
        assert result.source.account_id == source_case.account_id
        assert result.billing_account.scope == TenantScope(OPERATOR_TENANT_ID)
        assert result.billing_account.account_id == source_case.billing_account_id
        assert result.billing_account.external_account_ref == str(
            source_case.account_id
        )
        assert result.billing_account.currency == Currency("NGN", 2)
        assert not db.new and not db.dirty
        with pytest.raises(FrozenInstanceError):
            result.source = result.source
        with pytest.raises(FrozenInstanceError):
            result.billing_account.external_account_ref = "unrelated"
        assert (
            db.scalar(
                text("SELECT count(*) FROM public.module_invoice_cash_correlations")
            )
            == 0
        )
        assert (
            db.scalar(
                text(
                    "SELECT count(*) FROM mod_payments.payment_confirmations WHERE intent_id=:id"
                ),
                {"id": result.source.module_intent_id},
            )
            == 0
        )
        assert (
            db.scalar(
                text(
                    "SELECT count(*) FROM mod_billing.confirmed_settlements WHERE billing_account_id=:id"
                ),
                {"id": source_case.billing_account_id},
            )
            == 0
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE mod_billing.billing_accounts SET external_account_ref='unrelated-customer' WHERE id=:id",
        "UPDATE mod_billing.billing_accounts SET currency='USD' WHERE id=:id",
        "UPDATE mod_billing.billing_accounts SET minor_units=3 WHERE id=:id",
    ],
)
def test_candidate_account_consumer_reobserves_and_refuses_current_identity_drift(
    source_case: SourceCase, mutation: str
) -> None:
    query = ModuleInvoiceBillingAccountQuery(
        observation_id=source_case.query.observation_id,
        checkout_reference=source_case.query.checkout_reference,
    )
    with Session(source_case.runtime) as db, db.begin():
        original = get_module_invoice_billing_account(db, query)
    # Billing account identity is mutable. Privileged setup changes that real
    # account; the consumer must re-read the genuine public API, not a mock or
    # cached earlier observation. No binding trigger is bypassed or disabled.
    with source_case.admin.begin() as connection:
        connection.execute(
            text(mutation),
            {"id": source_case.billing_account_id},
        )
    with (
        Session(source_case.runtime) as db,
        db.begin(),
        pytest.raises(
            ModuleInvoiceCashSourceError,
            match="Current Billing account identity differs",
        ),
    ):
        get_module_invoice_billing_account(db, query)
    assert original.billing_account.external_account_ref == str(source_case.account_id)
    assert original.billing_account.currency == Currency("NGN", 2)
