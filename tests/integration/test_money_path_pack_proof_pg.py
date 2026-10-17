"""Step-3 money-path proof for the pinned Starter pack in Sub's composed DB.

One full path, driven only through the published module services at their
pinned versions (billing 0.1.0a8, payments 0.1.0a3, numbering 0.1.0a3 on
kernel 0.1.0a105): account -> rated invoice -> payment intent bound to the
invoice (the renewal pay-link target) -> provider confirmation evidence ->
confirmed-cash observation -> billing settlement acceptance bound to that
confirmation -> allocation discharging the invoice -> zero collectible
position. Pay-then-record is enforced negatively: no settlement is accepted
before payments confirmation evidence exists, and one confirmation cannot
fund two settlements.

This is a composition proof, not an authority switch. Sub's incumbent
billing/payment writers remain authoritative; no Sub caller is rewired here.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic.config import Config
from dotmac_billing import (
    AcceptRatedObligationV1,
    AcceptSettlementV1,
    AppliedFxSnapshotV1,
    AppliedTaxSnapshotV1,
    DueDateBasisV1,
    GetCurrentReceivablePositionV1,
    IssueRatedInvoiceV1,
    PartyDocumentSnapshotV1,
    PartyTaxIdentitySnapshotV1,
    PaymentInstructionsSnapshotV1,
    PostalAddressSnapshotV1,
    PresentationAssetReferenceV1,
    ServicePeriodEvidenceV1,
    SettlementFundingLane,
    get_current_receivable_position,
    issue_rated_invoice,
)
from dotmac_billing.commands import AllocationCommand
from dotmac_billing.contracts import DueDateBasisStatus, ServicePeriodStatus
from dotmac_billing.errors import BillingConflict, BillingRuleViolation
from dotmac_billing.service import (
    accept_settlement,
    allocate_settlement,
    create_billing_account,
)
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money
from dotmac_numbering import SeriesConfiguration, allocate, configure_series
from dotmac_payments import (
    ConfirmationNotFound,
    ConfirmationSource,
    GetConfirmedCashV1,
    OpenPaymentIntent,
    PaymentPurpose,
    RecordConfirmation,
    get_confirmed_cash_v1,
    open_payment_intent,
    record_confirmation,
)
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.services.operator_tenant import OPERATOR_TENANT_ID
from tests.integration import test_kernel_lineage_rehearsal as kernel_rehearsal

isolated_database = kernel_rehearsal.isolated_database

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
PERIOD_START = datetime(2026, 10, 1, tzinfo=UTC)
PERIOD_END = datetime(2026, 10, 31, 23, 59, tzinfo=UTC)
SCOPE = TenantScope(OPERATOR_TENANT_ID)
ACCEPTED_EVIDENCE = frozenset({"dotmac-payments:confirmed-cash-v1"})


def _render(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def _config(database_url: URL) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", _render(database_url))
    return config


def _money(amount: str) -> Money:
    return Money.of(amount, Currency("NGN", 2))


class _Sources:
    """Assembly obligation-source registry (Sub owns source declarations)."""

    def require_obligation_source(self, code: str) -> str:
        if code != "recurring_charge_occurrence":
            raise ValueError("undeclared obligation source")
        return "subscriptions"


class _Numbering:
    """Billing NumberingProvider adapter over the pinned numbering module."""

    def allocate(self, db: Session, **kwargs: object) -> str:
        return allocate(db, **kwargs).formatted_number


@contextmanager
def _tenant_session(database_url: URL) -> Iterator[Session]:
    engine = sa.create_engine(database_url)

    @sa.event.listens_for(engine, "connect")
    def _runtime_role(dbapi_connection, _record) -> None:
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET ROLE app_user")
        dbapi_connection.commit()

    try:
        with sessionmaker(bind=engine)() as session:
            session.execute(
                sa.text("SELECT set_config('app.current_tenant', :tenant, true)"),
                {"tenant": str(OPERATOR_TENANT_ID)},
            )
            yield session
    finally:
        sa.event.remove(engine, "connect", _runtime_role)
        engine.dispose()


def _issue_command(account_id: UUID, *, source_fact_id: str) -> IssueRatedInvoiceV1:
    basis = DueDateBasisV1(
        status=DueDateBasisStatus.VERIFIED,
        source_authority="contract.v1",
        evidence_ref="contract:line:1:v1",
        payment_terms_code="net_30",
        payment_terms_version="1",
        issued_at=NOW,
        effective_at=NOW,
        timezone="Africa/Lagos",
        derivation_policy="calendar_days",
        derivation_version="1",
    )
    return IssueRatedInvoiceV1(
        obligation=AcceptRatedObligationV1(
            scope=SCOPE,
            billing_account_id=account_id,
            contract_line_ref="contract-line-1",
            contract_version="v1",
            charge_component="recurring_access",
            source_system="subscriptions",
            source_kind="recurring_charge_occurrence",
            source_fact_id=source_fact_id,
            source_fact_version="1",
            subject_ref="customer:proof",
            service_ref="service:proof",
            service_period=ServicePeriodEvidenceV1(
                status=ServicePeriodStatus.VERIFIED,
                starts_at=PERIOD_START,
                ends_at=PERIOD_END,
            ),
            collection_timing="arrears",
            pre_tax_amount=_money("100.00"),
            tax_amount=_money("20.00"),
            total_amount=_money("120.00"),
            rated_at=NOW,
            price_version_id="price:v1",
            tax_snapshots=(
                AppliedTaxSnapshotV1(
                    treatment_code="standard",
                    jurisdiction_code="source-supplied-zone",
                    policy_id="tax-policy",
                    policy_version="1",
                    rate=Decimal("0.200000"),
                    taxable_basis=_money("100.00"),
                    tax_amount=_money("20.00"),
                ),
            ),
            fx_snapshot=AppliedFxSnapshotV1(
                observation_id="fx-proof-1",
                observation_version="1",
                base_currency="NGN",
                quote_currency="NGN",
                rate=Decimal("1.000000"),
                rate_purpose="document_currency",
                observed_at=NOW,
                effective_at=NOW,
                rounding_policy="minor_units",
                provenance="source-supplied",
            ),
        ),
        description="Monthly access",
        unit_code="month",
        seller_snapshot=PartyDocumentSnapshotV1(
            legal_name="Dotmac Technologies Ltd",
            address=PostalAddressSnapshotV1(
                line_one="Plot 1", city="Abuja", country_code="NGA"
            ),
        ),
        customer_snapshot=PartyDocumentSnapshotV1(
            legal_name="Customer Ltd",
            address=PostalAddressSnapshotV1(
                line_one="Plot 2", city="Abuja", country_code="NGA"
            ),
        ),
        payment_instructions=PaymentInstructionsSnapshotV1(
            method_code="bank_transfer",
            bank_name="Source supplied bank",
            account_name="Dotmac Technologies Ltd",
            account_reference="source-supplied-reference",
        ),
        brand_asset=PresentationAssetReferenceV1.none(),
        locale="en-NG",
        timezone="Africa/Lagos",
        document_profile_code="invoice.standard",
        document_profile_version="1",
        series_code="invoice",
        reference_date=date(2026, 10, 17),
        due_at=NOW + timedelta(days=30),
        due_date_basis=basis,
        actor_ref="money-path-proof",
        correlation_id="money-path-proof",
        party_tax_identities=(
            PartyTaxIdentitySnapshotV1(
                party_role="seller",
                identity_type="tax_id",
                identity_value="snapshot-value",
                country_code="NGA",
                source_authority="party.v1",
                source_version="1",
            ),
        ),
    )


def _collectible(database_url: URL, account_id: UUID) -> Decimal:
    with _tenant_session(database_url) as session:
        current = get_current_receivable_position(
            session,
            scope=SCOPE,
            request=GetCurrentReceivablePositionV1(
                scope=SCOPE,
                billing_account_id=account_id,
                currency=Currency("NGN", 2),
            ),
        )
        assert current.position is not None
        return current.position.collectible_receivable.amount


def _settlement(account_id: UUID, *, key: str, evidence: str) -> AcceptSettlementV1:
    return AcceptSettlementV1(
        scope=SCOPE,
        billing_account_id=account_id,
        source_system="dotmac-payments",
        source_settlement_key=key,
        source_version="1",
        amount=_money("120.00"),
        occurred_at=NOW,
        observed_at=NOW,
        confirmation_evidence=evidence,
        funding_lane=SettlementFundingLane.AVAILABLE_CREDIT,
    )


def test_money_path_pay_qualify_fund_invoice_renew(isolated_database: URL) -> None:
    database_url = isolated_database
    command.upgrade(_config(database_url), "heads")

    # Account, numbering series and the rated renewal invoice (Starter billing).
    with _tenant_session(database_url) as session:
        configure_series(
            session,
            scope=SCOPE,
            configuration=SeriesConfiguration(
                series_code="invoice", prefix="INV", min_digits=6
            ),
        )
        account = create_billing_account(
            session,
            scope=SCOPE,
            external_account_ref=f"money-path-{uuid4()}",
            currency="NGN",
            minor_units=2,
        )
        account_id = account.id
        issued = issue_rated_invoice(
            session,
            scope=SCOPE,
            command=_issue_command(account_id, source_fact_id=f"occ-{uuid4()}"),
            obligation_sources=_Sources(),
            numbering=_Numbering(),
        )
        invoice_id = issued.invoice_id
        session.commit()
    assert _collectible(database_url, account_id) == Decimal("120.00")

    # Pay link: an intent bound to the invoice, before any provider I/O.
    reference = f"paylink:{invoice_id}"
    with _tenant_session(database_url) as session:
        intent = open_payment_intent(
            session,
            scope=SCOPE,
            command=OpenPaymentIntent(
                reference=reference,
                payer_reference=str(account_id),
                purpose=PaymentPurpose.INVOICE_SETTLEMENT,
                target_reference=str(invoice_id),
                requested=_money("120.00"),
                provider_type="PAYSTACK",
                channel="WEB",
                opened_at=NOW,
                expires_at=NOW + timedelta(hours=24),
            ),
        )
        intent_id = intent.id
        session.commit()

    # Pay-then-record: without payments confirmation evidence there is no
    # confirmed cash to fund a settlement, and billing refuses undeclared
    # evidence. A browser return alone can never reach either call.
    with _tenant_session(database_url) as session:
        with pytest.raises(ConfirmationNotFound):
            get_confirmed_cash_v1(
                session, scope=SCOPE, query=GetConfirmedCashV1(uuid4())
            )
        session.rollback()
    with _tenant_session(database_url) as session:
        with pytest.raises(BillingRuleViolation):
            accept_settlement(
                session,
                scope=SCOPE,
                command=_settlement(
                    account_id, key="premature", evidence="browser_return"
                ),
                accepted_confirmation_evidence=ACCEPTED_EVIDENCE,
            )
        session.rollback()

    # Authenticated provider evidence confirms the intent (payments owns
    # qualification; the browser return recorded nothing above).
    with _tenant_session(database_url) as session:
        confirmation = record_confirmation(
            session,
            scope=SCOPE,
            command=RecordConfirmation(
                intent_id=intent_id,
                source=ConfirmationSource.PROVIDER_CALLBACK,
                external_reference="paystack-txn-proof-1",
                confirmed=_money("120.00"),
                observed_at=NOW,
            ),
        )
        confirmation_id = confirmation.id
        session.commit()

    # Confirmed cash funds exactly one settlement, which discharges the
    # invoice on allocation. The billing source key IS the payments
    # confirmation identity, so the cash-source claim is traceable.
    with _tenant_session(database_url) as session:
        cash = get_confirmed_cash_v1(
            session, scope=SCOPE, query=GetConfirmedCashV1(confirmation_id)
        )
        assert cash.intent_id == intent_id
        assert cash.confirmed.amount == Decimal("120.00")
        settlement = accept_settlement(
            session,
            scope=SCOPE,
            command=_settlement(
                account_id,
                key=str(confirmation_id),
                evidence="dotmac-payments:confirmed-cash-v1",
            ),
            accepted_confirmation_evidence=ACCEPTED_EVIDENCE,
        )
        allocate_settlement(
            session,
            scope=SCOPE,
            command=AllocationCommand(
                settlement_id=settlement.id,
                document_id=invoice_id,
                amount=_money("120.00"),
                occurred_at=NOW,
                source_ref=f"payments-confirmation:{confirmation_id}",
            ),
        )
        session.commit()
    assert _collectible(database_url, account_id) == Decimal("0.00")

    # One confirmation funds one settlement: a second acceptance under the
    # same confirmation identity (even against a fresh account) is refused.
    with _tenant_session(database_url) as session:
        other = create_billing_account(
            session,
            scope=SCOPE,
            external_account_ref=f"money-path-other-{uuid4()}",
            currency="NGN",
            minor_units=2,
        )
        with pytest.raises(BillingConflict):
            accept_settlement(
                session,
                scope=SCOPE,
                command=_settlement(
                    other.id,
                    key=str(confirmation_id),
                    evidence="dotmac-payments:confirmed-cash-v1",
                ),
                accepted_confirmation_evidence=ACCEPTED_EVIDENCE,
            )
        session.rollback()
