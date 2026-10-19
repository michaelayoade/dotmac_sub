"""Available-credit lane proof for the pinned Starter pack (Slice 7 shape).

The Step-3 money path allocated a settlement directly to its invoice.
Account-credit deposits take the other lane: confirmed cash is accepted
into the available-credit lane first, then drawn into invoices with
``apply_available_credit`` — never creating cash. This proves that lane
end to end on Sub's migrated PostgreSQL at the pinned versions:
deposit intent -> provider confirmation -> settlement into credit lane
-> two exact draws across two invoices -> an over-draw refusal, with
both position lanes asserted at every step.

Proof only: no Sub caller is rewired and no authority changes hands.
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
from dotmac_billing.commands import ApplyAvailableCreditCommand
from dotmac_billing.contracts import DueDateBasisStatus, ServicePeriodStatus
from dotmac_billing.errors import BillingRuleViolation
from dotmac_billing.service import (
    accept_settlement,
    apply_available_credit,
    create_billing_account,
)
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money
from dotmac_numbering import SeriesConfiguration, allocate, configure_series
from dotmac_payments import (
    ConfirmationSource,
    OpenPaymentIntent,
    PaymentPurpose,
    RecordConfirmation,
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


def _config(database_url: URL) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option(
        "sqlalchemy.url", database_url.render_as_string(hide_password=False)
    )
    return config


def _money(amount: str) -> Money:
    return Money.of(amount, Currency("NGN", 2))


class _Sources:
    def require_obligation_source(self, code: str) -> str:
        if code != "recurring_charge_occurrence":
            raise ValueError("undeclared obligation source")
        return "subscriptions"


class _Numbering:
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


def _party(name: str, line_one: str) -> PartyDocumentSnapshotV1:
    return PartyDocumentSnapshotV1(
        legal_name=name,
        address=PostalAddressSnapshotV1(
            line_one=line_one, city="Abuja", country_code="NGA"
        ),
    )


def _issue_command(account_id: UUID, *, suffix: str) -> IssueRatedInvoiceV1:
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
            source_fact_id=f"occ-{suffix}-{uuid4()}",
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
                observation_id="fx-proof",
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
        seller_snapshot=_party("Dotmac Technologies Ltd", "Plot 1"),
        customer_snapshot=_party("Customer Ltd", "Plot 2"),
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
        actor_ref="credit-lane-proof",
        correlation_id=f"credit-lane-proof-{suffix}",
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


def _lanes(database_url: URL, account_id: UUID) -> tuple[Decimal, Decimal]:
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
        return (
            current.position.collectible_receivable.amount,
            current.position.available_credit.amount,
        )


def test_deposit_funds_credit_lane_then_draws_exactly(
    isolated_database: URL,
) -> None:
    database_url = isolated_database
    command.upgrade(_config(database_url), "heads")

    # Two invoices of 120.00 each on one account.
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
            external_account_ref=f"credit-lane-{uuid4()}",
            currency="NGN",
            minor_units=2,
        )
        account_id = account.id
        invoice_ids = []
        for suffix in ("a", "b"):
            issued = issue_rated_invoice(
                session,
                scope=SCOPE,
                command=_issue_command(account_id, suffix=suffix),
                obligation_sources=_Sources(),
                numbering=_Numbering(),
            )
            invoice_ids.append(issued.invoice_id)
        session.commit()
    assert _lanes(database_url, account_id) == (Decimal("240.00"), Decimal("0.00"))

    # A 200.00 account-credit deposit: intent, provider evidence, then the
    # settlement lands in the credit lane without touching either invoice.
    with _tenant_session(database_url) as session:
        intent = open_payment_intent(
            session,
            scope=SCOPE,
            command=OpenPaymentIntent(
                reference=f"deposit:{uuid4()}",
                payer_reference=str(account_id),
                purpose=PaymentPurpose.ACCOUNT_CREDIT_DEPOSIT,
                requested=_money("200.00"),
                provider_type="PAYSTACK",
                channel="WEB",
                opened_at=NOW,
                expires_at=NOW + timedelta(hours=24),
            ),
        )
        confirmation = record_confirmation(
            session,
            scope=SCOPE,
            command=RecordConfirmation(
                intent_id=intent.id,
                source=ConfirmationSource.PROVIDER_CALLBACK,
                external_reference="paystack-deposit-proof-1",
                confirmed=_money("200.00"),
                observed_at=NOW,
            ),
        )
        accept_settlement(
            session,
            scope=SCOPE,
            command=AcceptSettlementV1(
                scope=SCOPE,
                billing_account_id=account_id,
                source_system="dotmac-payments",
                source_settlement_key=str(confirmation.id),
                source_version="1",
                amount=_money("200.00"),
                occurred_at=NOW,
                observed_at=NOW,
                confirmation_evidence="dotmac-payments:confirmed-cash-v1",
                funding_lane=SettlementFundingLane.AVAILABLE_CREDIT,
            ),
            accepted_confirmation_evidence=ACCEPTED_EVIDENCE,
        )
        session.commit()
    assert _lanes(database_url, account_id) == (
        Decimal("240.00"),
        Decimal("200.00"),
    )

    # Exact draws: settle invoice A in full, then part-settle invoice B.
    with _tenant_session(database_url) as session:
        apply_available_credit(
            session,
            scope=SCOPE,
            command=ApplyAvailableCreditCommand(
                billing_account_id=account_id,
                document_id=invoice_ids[0],
                amount=_money("120.00"),
                occurred_at=NOW,
                source_ref="credit-lane-proof:draw-a",
            ),
        )
        session.commit()
    assert _lanes(database_url, account_id) == (
        Decimal("120.00"),
        Decimal("80.00"),
    )

    with _tenant_session(database_url) as session:
        apply_available_credit(
            session,
            scope=SCOPE,
            command=ApplyAvailableCreditCommand(
                billing_account_id=account_id,
                document_id=invoice_ids[1],
                amount=_money("80.00"),
                occurred_at=NOW,
                source_ref="credit-lane-proof:draw-b",
            ),
        )
        session.commit()
    assert _lanes(database_url, account_id) == (Decimal("40.00"), Decimal("0.00"))

    # The lane is exhausted: one more kobo of draw is refused, creating
    # no cash and moving no position.
    with _tenant_session(database_url) as session:
        with pytest.raises(BillingRuleViolation):
            apply_available_credit(
                session,
                scope=SCOPE,
                command=ApplyAvailableCreditCommand(
                    billing_account_id=account_id,
                    document_id=invoice_ids[1],
                    amount=_money("0.01"),
                    occurred_at=NOW,
                    source_ref="credit-lane-proof:draw-over",
                ),
            )
        session.rollback()
    assert _lanes(database_url, account_id) == (Decimal("40.00"), Decimal("0.00"))
