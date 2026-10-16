"""Hosted foundation canaries; no cash acceptance or final handoff is claimed.

Real Alembic heads and an actual app_user login are mandatory. The shared
pristine hosted fixture owns temporary test authentication and cleanup; no
financial privilege/role repair or fabricated approval is introduced.
Positive confirmation/posting/concurrency tests await the qualified physical
merchant namespace and published exact Billing assembly.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic.config import Config
from dotmac_billing.service import create_billing_account
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
    PaymentProviderEventFinancialEffect,
    PaymentProviderEventSource,
    PaymentProviderEventStatus,
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
    RECONCILIATION_PARTICIPANT_SCOPE,
    WEBHOOK_PARTICIPANT_SCOPE,
    PaymentProviderEventCommand,
    PaymentProviderEventError,
    PaymentProviderEventResult,
    PaymentProviderEvents,
)
from app.services.provider_payment_settlements import (
    ModuleInvoiceCashSourceQuery,
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
    "runtime", [True, False], ids=("actual_app_login", "setup_owner")
)
@pytest.mark.parametrize(
    "mutation",
    [
        "id = '00000000-0000-0000-0000-000000000001'",
        "provider_id = :replacement_provider_id",
        "event_type = 'gateway.reconciliation.succeeded'",
        "external_id = 'other-transaction'",
        "idempotency_key = 'other-key'",
        "source = 'gateway_reconciliation'",
        "source = 'legacy_unknown'",
        "observation_digest = NULL",
        "observed_payment_status = 'failed'",
        "amount = 103",
        "provider_fee = 3",
        "net_amount = 99",
        "provider_reference = 'other-checkout'",
        "currency = 'USD'",
        "financial_effect = 'refund_confirmed'",
        "occurred_at = NULL",
        "occurred_at = received_at",
        "received_at = received_at + interval '1 second'",
    ],
)
def test_verified_source_facts_refuse_direct_mutation(
    source_case: SourceCase, mutation: str, runtime: bool
) -> None:
    engine = source_case.runtime if runtime else source_case.admin
    replacement_provider_id = source_case.provider_id
    if mutation == "provider_id = :replacement_provider_id":
        # Keep the replacement FK valid: AFTER constraint triggers may execute
        # before the integrity guard, and a missing provider proves only FK refusal.
        with Session(source_case.admin) as db, db.begin():
            replacement = PaymentProvider(
                name=f"source-mutation-{uuid4()}",
                provider_type=PaymentProviderType.paystack,
            )
            db.add(replacement)
            db.flush()
            replacement_provider_id = replacement.id
    with (
        pytest.raises(DBAPIError, match="verified provider source facts are immutable"),
        engine.begin() as connection,
    ):
        connection.execute(
            text(f"UPDATE public.payment_provider_events SET {mutation} WHERE id=:id"),  # noqa: S608 - closed parametrized SQL
            {
                "id": source_case.query.observation_id,
                "replacement_provider_id": replacement_provider_id,
            },
        )
    with Session(source_case.runtime) as db, db.begin():
        source = get_module_invoice_cash_source(db, source_case.query)
        assert source.observation_id == source_case.query.observation_id
        assert source.net_amount == Decimal("100.00")


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


def test_guard_ignores_hidden_tenant_bindings_and_rolls_back_processing(
    source_case: SourceCase,
) -> None:
    with source_case.admin.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.module_billing_account_bindings")
            )
            == 1
        )
    with (
        pytest.raises(DBAPIError, match="verified provider source facts are immutable"),
        source_case.runtime.begin() as connection,
    ):
        connection.execute(
            text("SELECT set_config('app.current_tenant', :tenant, true)"),
            {"tenant": str(uuid4())},
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.module_billing_account_bindings")
            )
            == 0
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM public.module_invoice_cash_correlations")
            )
            == 0
        )
        connection.execute(
            text(
                "UPDATE public.payment_provider_events SET error_code='must_rollback' WHERE id=:id"
            ),
            {"id": source_case.query.observation_id},
        )
        connection.execute(
            text("UPDATE public.payment_provider_events SET amount=103 WHERE id=:id"),
            {"id": source_case.query.observation_id},
        )
    with source_case.runtime.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT error_code FROM public.payment_provider_events WHERE id=:id"
                ),
                {"id": source_case.query.observation_id},
            )
            is None
        )


def test_same_values_equivalent_instant_and_processing_updates_survive(
    source_case: SourceCase,
) -> None:
    with source_case.runtime.begin() as connection:
        connection.execute(text("SET LOCAL TIME ZONE 'Africa/Lagos'"))
        connection.execute(
            text("""
            UPDATE public.payment_provider_events
            SET amount=amount, source=source, observation_digest=observation_digest,
                occurred_at='2026-10-15 09:01:00+01'::timestamptz,
                received_at=received_at, payment_id=NULL, invoice_id=NULL,
                status='failed', processed_at=now(), error_code='retry', error='retry',
                payload='{"delivery":"retry"}'::json
            WHERE id=:id
        """),
            {"id": source_case.query.observation_id},
        )
    with Session(source_case.runtime) as db, db.begin():
        source = get_module_invoice_cash_source(db, source_case.query)
        assert source.occurred_at == source_case.occurred_at
        row = db.get(PaymentProviderEvent, source.observation_id)
        assert row is not None
        assert row.payload == {"delivery": "retry"}
        assert row.status is PaymentProviderEventStatus.failed
        assert row.error_code == "retry"
        assert row.processed_at is not None


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM public.payment_provider_events",
        "TRUNCATE public.payment_provider_events CASCADE",
        "TRUNCATE public.payment_providers CASCADE",
    ],
)
def test_observation_removal_and_cascade_refuse(
    source_case: SourceCase, statement: str
) -> None:
    engine = source_case.admin
    with (
        pytest.raises(
            DBAPIError, match="provider observations cannot be deleted or truncated"
        ),
        engine.begin() as connection,
    ):
        connection.execute(text(statement))
    with source_case.runtime.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM public.payment_provider_events WHERE id=:id"
                ),
                {"id": source_case.query.observation_id},
            )
            == 1
        )


def test_empty_observations_cannot_be_truncated(
    runtime_connections: RuntimeConnections,
) -> None:
    admin = runtime_connections.admin
    assert admin.execute(
        "SELECT count(*) FROM public.payment_provider_events"
    ).fetchone() == (0,)
    with (
        pytest.raises(
            psycopg.errors.RestrictViolation, match="cannot be deleted or truncated"
        ),
        admin.transaction(),
    ):
        admin.execute("TRUNCATE public.payment_provider_events CASCADE")


def _verified_observation(
    db: Session, command: PaymentProviderEventCommand, *, webhook: bool
) -> PaymentProviderEventResult:
    method = (
        PaymentProviderEvents.stage_verified_webhook_event
        if webhook
        else PaymentProviderEvents.stage_verified_reconciliation_event
    )
    return method(
        db,
        command,
        context=CommandContext.system(
            actor="pytest:provider-source-freeze",
            scope=WEBHOOK_PARTICIPANT_SCOPE
            if webhook
            else RECONCILIATION_PARTICIPANT_SCOPE,
            reason="Verified observation replay canary; no matched payment or cash posting",
        ),
    )


@pytest.mark.parametrize("webhook_first", [True, False])
def test_actual_login_pending_verified_replay_preserves_first_facts(
    source_case: SourceCase, webhook_first: bool
) -> None:
    command = PaymentProviderEventCommand(
        provider_id=source_case.provider_id,
        event_type="charge.success",
        external_id=f"unmatched-{uuid4()}",
        idempotency_key=f"unmatched-key-{uuid4()}",
        amount=Decimal("100.00"),
        provider_fee=Decimal("1.50"),
        net_amount=Decimal("98.50"),
        currency="NGN",
        occurred_at=source_case.occurred_at,
        payload={"delivery": "first"},
    )
    with Session(source_case.runtime) as db, db.begin():
        first = _verified_observation(db, command, webhook=webhook_first)
        row = db.get(PaymentProviderEvent, first.id)
        assert row is not None
        row.status = PaymentProviderEventStatus.pending
    with Session(source_case.runtime) as db, db.begin():
        second = _verified_observation(
            db,
            replace(
                command,
                event_type="gateway.reconciliation.succeeded",
                payload={"delivery": "retry"},
            ),
            webhook=not webhook_first,
        )
        assert second.id == first.id
        assert not second.replayed
        assert second.source is first.source
        assert second.observation_digest == first.observation_digest
        assert second.received_at == first.received_at
        row = db.get(PaymentProviderEvent, first.id)
        assert row is not None
        assert row.event_type == command.event_type
        assert row.payload == {"delivery": "retry"}
        assert row.payment_id is None
        assert row.error_code == "payment_not_found"
        replay = _verified_observation(db, command, webhook=webhook_first)
        assert replay.replayed


@pytest.mark.parametrize("pending", [True, False])
def test_actual_login_legacy_enrichment_then_source_freeze(
    source_case: SourceCase, pending: bool
) -> None:
    command = PaymentProviderEventCommand(
        provider_id=source_case.provider_id,
        event_type="charge.success",
        external_id=f"legacy-unmatched-{uuid4()}",
        amount=Decimal("100.00"),
        provider_fee=Decimal("1.50"),
        net_amount=Decimal("98.50"),
        currency="NGN",
    )
    with Session(source_case.runtime) as db, db.begin():
        legacy = PaymentProviderEvent(
            provider_id=command.provider_id,
            event_type=command.event_type,
            external_id=command.external_id,
            amount=command.amount,
            currency=command.currency,
            source=PaymentProviderEventSource.legacy_unknown,
            financial_effect=PaymentProviderEventFinancialEffect.none,
            status=PaymentProviderEventStatus.pending
            if pending
            else PaymentProviderEventStatus.failed,
        )
        db.add(legacy)
        db.flush()
        legacy_id = legacy.id
    with Session(source_case.runtime) as db, db.begin():
        with pytest.raises(PaymentProviderEventError, match="different evidence"):
            _verified_observation(
                db, replace(command, amount=Decimal("101.00")), webhook=True
            )
        legacy = db.get(PaymentProviderEvent, legacy_id)
        assert legacy is not None
        assert legacy.source is PaymentProviderEventSource.legacy_unknown
        result = _verified_observation(db, command, webhook=True)
        assert result.id == legacy_id
        assert result.source is PaymentProviderEventSource.verified_webhook
        assert result.observation_digest is not None
        assert legacy.provider_fee == Decimal("1.50")
        assert legacy.net_amount == Decimal("98.50")
        assert legacy.payment_id is None
    with (
        pytest.raises(DBAPIError, match="source facts are immutable"),
        source_case.runtime.begin() as connection,
    ):
        connection.execute(
            text(
                "UPDATE public.payment_provider_events SET net_amount=97 WHERE id=:id"
            ),
            {"id": legacy_id},
        )


def test_requested_credit_and_provider_money_are_independent_observations(
    source_case: SourceCase,
) -> None:
    # Setup seeds facts at INSERT, never disables the freeze or rewrites history.
    with Session(source_case.admin) as db, db.begin():
        event = PaymentProviderEvent(
            provider_id=source_case.provider_id,
            event_type="charge.success",
            external_id=f"different-money-{uuid4()}",
            source=PaymentProviderEventSource.verified_webhook,
            observation_digest="b" * 64,
            observed_payment_status=PaymentStatus.succeeded,
            amount=Decimal("105.00"),
            provider_fee=Decimal("4.00"),
            net_amount=Decimal("99.00"),
            currency="NGN",
            provider_reference=source_case.query.checkout_reference,
            occurred_at=source_case.occurred_at,
            received_at=source_case.observed_at,
        )
        db.add(event)
        db.flush()
        query = ModuleInvoiceCashSourceQuery(
            event.id, source_case.query.checkout_reference
        )
    with Session(source_case.runtime) as db, db.begin():
        source = get_module_invoice_cash_source(db, query)
        assert source.requested_customer_credit_amount == Decimal("100.00")
        assert source.gross_amount == Decimal("105.00")
        assert source.provider_fee == Decimal("4.00")
        assert source.net_amount == Decimal("99.00")
        for table in (
            "mod_payments.payment_confirmations",
            "mod_billing.confirmed_settlements",
            "public.module_invoice_cash_correlations",
        ):
            assert db.scalar(text(f"SELECT count(*) FROM {table}")) == 0  # noqa: S608 - fixed local table names


def test_actual_login_delete_guard_and_new_observation_atomic_rollback(
    source_case: SourceCase,
) -> None:
    with (
        pytest.raises(DBAPIError, match="cannot be deleted or truncated"),
        source_case.runtime.begin() as connection,
    ):
        connection.execute(
            text("DELETE FROM public.payment_provider_events WHERE id=:id"),
            {"id": source_case.query.observation_id},
        )
    command = PaymentProviderEventCommand(
        provider_id=source_case.provider_id,
        event_type="charge.success",
        external_id=f"rollback-unmatched-{uuid4()}",
        amount=Decimal("100.00"),
        currency="NGN",
        net_amount=Decimal("100.00"),
    )
    with source_case.runtime.connect() as connection:
        before = tuple(
            connection.scalar(text(f"SELECT count(*) FROM public.{table}"))  # noqa: S608 - fixed local table names
            for table in ("payment_provider_events", "audit_events", "event_store")
        )
    with (
        pytest.raises(RuntimeError, match="rollback canary"),
        Session(source_case.runtime) as db,
        db.begin(),
    ):
        result = _verified_observation(db, command, webhook=True)
        assert result.observation_digest is not None
        db.flush()
        raise RuntimeError("rollback canary")
    with source_case.runtime.connect() as connection:
        after = tuple(
            connection.scalar(text(f"SELECT count(*) FROM public.{table}"))  # noqa: S608 - fixed local table names
            for table in ("payment_provider_events", "audit_events", "event_store")
        )
        assert after == before


def test_693_real_predecessor_upgrade_preserves_rows_and_existing_guards(
    cloned_database: Callable[[str], URL],
) -> None:
    engine = create_engine(cloned_database("692_module_invoice_cash"))
    try:
        with Session(engine) as db, db.begin():
            provider = PaymentProvider(
                name=f"freeze-upgrade-{uuid4()}",
                provider_type=PaymentProviderType.custom,
            )
            db.add(provider)
            db.flush()
            event = PaymentProviderEvent(
                provider_id=provider.id,
                event_type="charge.success",
                source=PaymentProviderEventSource.verified_webhook,
                observation_digest="c" * 64,
                amount=Decimal("100.00"),
                occurred_at=None,
            )
            legacy = PaymentProviderEvent(provider_id=provider.id, event_type="legacy")
            db.add_all([event, legacy])
            db.flush()
        with engine.connect() as connection:
            rows_before = (
                connection.execute(
                    text(
                        "SELECT row_to_json(e)::text FROM public.payment_provider_events e ORDER BY id"
                    )
                )
                .scalars()
                .all()
            )
            triggers_before = connection.execute(
                text("""
                SELECT oid, tgname, tgenabled, pg_catalog.pg_get_triggerdef(oid)
                FROM pg_catalog.pg_trigger WHERE NOT tgisinternal ORDER BY oid
            """)
            ).all()
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        alembic_command.upgrade(config, "693_provider_source_facts")
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT row_to_json(e)::text FROM public.payment_provider_events e ORDER BY id"
                    )
                )
                .scalars()
                .all()
                == rows_before
            )
            triggers_after = connection.execute(
                text("""
                SELECT oid, tgname, tgenabled, pg_catalog.pg_get_triggerdef(oid)
                FROM pg_catalog.pg_trigger WHERE NOT tgisinternal ORDER BY oid
            """)
            ).all()
            assert all(row in triggers_after for row in triggers_before)
            assert len(triggers_after) == len(triggers_before) + 2
            guards = connection.execute(
                text("""
                SELECT t.tgname, t.tgenabled, t.tgtype, f.prosecdef, f.proconfig
                FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_proc f ON f.oid=t.tgfoid
                WHERE t.tgrelid='public.payment_provider_events'::regclass
                  AND t.tgname IN ('verified_provider_source_facts_immutable', 'provider_source_facts_no_truncate')
                ORDER BY t.tgname
            """)
            ).all()
            assert [tuple(row) for row in guards] == [
                (
                    "provider_source_facts_no_truncate",
                    "A",
                    34,
                    False,
                    ["search_path=pg_catalog"],
                ),
                (
                    "verified_provider_source_facts_immutable",
                    "A",
                    25,
                    False,
                    ["search_path=pg_catalog"],
                ),
            ]
            assert connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM public.alembic_version WHERE version_num='693_provider_source_facts')"
                )
            )
        with (
            pytest.raises(DBAPIError, match="source facts are immutable"),
            engine.begin() as connection,
        ):
            connection.execute(
                text(
                    "UPDATE public.payment_provider_events SET amount=101 WHERE source='verified_webhook'"
                )
            )
        with pytest.raises(RuntimeError, match="forward-only"):
            alembic_command.downgrade(config, "692_module_invoice_cash")
        with engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM public.alembic_version WHERE version_num='693_provider_source_facts')"
                )
            )
        # Legacy data is preserved and can receive its first verified evidence.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE public.payment_provider_events SET source='verified_webhook', observation_digest=:digest WHERE source='legacy_unknown'"
                ),
                {"digest": "d" * 64},
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("mutates_source", [False, True], ids=("amount", "source"))
def test_final_tuple_guard_refuses_later_before_trigger_fact_mutation(
    source_case: SourceCase, mutates_source: bool
) -> None:
    """Source integrity under actual login, not global helper/privilege closure."""
    event_id = source_case.query.observation_id
    with source_case.runtime.begin() as connection:
        assert connection.execute(text("SELECT session_user, current_user")).one() == (
            "app_user",
            "app_user",
        )
        # Positive control: the exact lifecycle update is allowed before the
        # canary mutator is installed. Neither integrity guard is disabled.
        connection.execute(
            text(
                "UPDATE public.payment_provider_events SET error_code='late_before_canary' WHERE id=:id"
            ),
            {"id": event_id},
        )
        connection.execute(
            text(
                "UPDATE public.payment_provider_events SET error_code=NULL WHERE id=:id"
            ),
            {"id": event_id},
        )
        before = connection.execute(
            text(
                "SELECT amount, source, status, error_code, error FROM public.payment_provider_events WHERE id=:id"
            ),
            {"id": event_id},
        ).one()
    with source_case.admin.begin() as connection:
        # The name sorts after the incumbent guard name: a BEFORE integrity
        # guard would accept the original tuple before this mutator changed it.
        function_sql = (
            """
            CREATE FUNCTION public.z_after_guard_canary_mutator() RETURNS trigger
            LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $$
            BEGIN
                IF NEW.error_code = 'late_before_canary' THEN
                    NEW.source := 'administrative_ingest';
                END IF;
                RETURN NEW;
            END $$
            """
            if mutates_source
            else """
            CREATE FUNCTION public.z_after_guard_canary_mutator() RETURNS trigger
            LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $$
            BEGIN
                IF NEW.error_code = 'late_before_canary' THEN
                    NEW.amount := OLD.amount + 1;
                END IF;
                RETURN NEW;
            END $$
            """
        )
        connection.execute(text(function_sql))
        connection.execute(
            text("""
            CREATE TRIGGER z_after_guard_canary_mutator
            BEFORE UPDATE ON public.payment_provider_events FOR EACH ROW
            EXECUTE FUNCTION public.z_after_guard_canary_mutator()
        """)
        )
        assert connection.execute(
            text("""
            SELECT tgname, tgtype, tgenabled FROM pg_catalog.pg_trigger
            WHERE tgrelid='public.payment_provider_events'::regclass
              AND tgname IN ('verified_provider_source_facts_immutable', 'z_after_guard_canary_mutator')
            ORDER BY tgname
        """)
        ).all() == [
            ("verified_provider_source_facts_immutable", 25, "A"),
            ("z_after_guard_canary_mutator", 19, "O"),
        ]
    with (
        pytest.raises(DBAPIError) as refusal,
        source_case.runtime.begin() as connection,
    ):
        connection.execute(
            text(
                "UPDATE public.payment_provider_events SET error='must_rollback' WHERE id=:id"
            ),
            {"id": event_id},
        )
        # The caller changes only an allowed lifecycle field. The exact guard
        # error proves the mutator ran and its final protected tuple was refused.
        connection.execute(
            text(
                "UPDATE public.payment_provider_events SET error_code='late_before_canary' WHERE id=:id"
            ),
            {"id": event_id},
        )
    assert isinstance(refusal.value.orig, psycopg.errors.RestrictViolation)
    assert refusal.value.orig.sqlstate == "23001"
    assert (
        refusal.value.orig.diag.message_primary
        == "verified provider source facts are immutable"
    )
    with source_case.runtime.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT amount, source, status, error_code, error FROM public.payment_provider_events WHERE id=:id"
                ),
                {"id": event_id},
            ).one()
            == before
        )
