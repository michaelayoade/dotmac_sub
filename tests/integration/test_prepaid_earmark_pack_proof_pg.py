"""Prepaid purchase-funding earmark proof for the pinned Starter pack.

Prepaid period purchases (Sub's largest billing caller family) fund
future periods in advance: one confirmed settlement is earmarked in
full to named future obligation source facts, and each period's
invoice may draw only its own earmarked target. This proves that
shape on Sub's migrated PostgreSQL at the pinned versions: purchase
intent -> provider confirmation -> prepaid-funding settlement ->
earmark to two period targets -> wrong-target allocation refused ->
each period invoice allocates exactly its target, with funding
availability asserted at every step.

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
    EarmarkSettlementV1,
    GetCurrentReceivablePositionV1,
    GetFundingAvailabilityV1,
    IssueRatedInvoiceV1,
    PartyDocumentSnapshotV1,
    PartyTaxIdentitySnapshotV1,
    PaymentInstructionsSnapshotV1,
    PostalAddressSnapshotV1,
    PresentationAssetReferenceV1,
    ServicePeriodEvidenceV1,
    SettlementEarmarkTargetV1,
    SettlementFundingLane,
    earmark_settlement,
    get_current_receivable_position,
    get_funding_availability,
    issue_rated_invoice,
)
from dotmac_billing.commands import AllocationCommand
from dotmac_billing.contracts import DueDateBasisStatus, ServicePeriodStatus
from dotmac_billing.errors import BillingRuleViolation
from dotmac_billing.service import (
    accept_settlement,
    allocate_settlement,
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


def _issue_command(
    account_id: UUID, *, suffix: str, source_fact_id: str
) -> IssueRatedInvoiceV1:
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
        actor_ref="prepaid-proof",
        correlation_id=f"prepaid-proof-{suffix}",
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
            current.position.prepaid_funding.amount,
        )


def _availability(
    database_url: URL, account_id: UUID, settlement_id: UUID
) -> tuple[Decimal, Decimal, Decimal]:
    with _tenant_session(database_url) as session:
        observed = get_funding_availability(
            session,
            scope=SCOPE,
            request=GetFundingAvailabilityV1(SCOPE, account_id, settlement_id),
        )
        return (
            observed.posted.amount,
            observed.held.amount,
            observed.unrestricted.amount,
        )


def test_purchase_funding_earmark_draws_per_period(
    isolated_database: URL,
) -> None:
    database_url = isolated_database
    command.upgrade(_config(database_url), "heads")
    purchase_ref = f"purchase:{uuid4()}"
    period_facts = (f"occ-p1-{uuid4()}", f"occ-p2-{uuid4()}")

    # Fund a two-period purchase (240.00) and earmark it in full.
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
            external_account_ref=f"prepaid-{uuid4()}",
            currency="NGN",
            minor_units=2,
        )
        account_id = account.id
        intent = open_payment_intent(
            session,
            scope=SCOPE,
            command=OpenPaymentIntent(
                reference=f"purchase-funding:{uuid4()}",
                payer_reference=str(account_id),
                purpose=PaymentPurpose.PURCHASE_FUNDING,
                target_reference=purchase_ref,
                requested=_money("240.00"),
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
                external_reference="paystack-purchase-proof-1",
                confirmed=_money("240.00"),
                observed_at=NOW,
            ),
        )
        settlement = accept_settlement(
            session,
            scope=SCOPE,
            command=AcceptSettlementV1(
                scope=SCOPE,
                billing_account_id=account_id,
                source_system="dotmac-payments",
                source_settlement_key=str(confirmation.id),
                source_version="1",
                amount=_money("240.00"),
                occurred_at=NOW,
                observed_at=NOW,
                confirmation_evidence="dotmac-payments:confirmed-cash-v1",
                funding_lane=SettlementFundingLane.PREPAID_FUNDING,
            ),
            accepted_confirmation_evidence=ACCEPTED_EVIDENCE,
        )
        settlement_id = settlement.id
        earmark_settlement(
            session,
            scope=SCOPE,
            command=EarmarkSettlementV1(
                SCOPE,
                account_id,
                settlement_id,
                _money("240.00"),
                purchase_ref,
                tuple(
                    SettlementEarmarkTargetV1(
                        "subscriptions",
                        "recurring_charge_occurrence",
                        fact,
                        "1",
                        _money("120.00"),
                    )
                    for fact in period_facts
                ),
                f"earmark:{uuid4()}",
                NOW,
            ),
        )
        session.commit()
    assert _availability(database_url, account_id, settlement_id) == (
        Decimal("240.00"),
        Decimal("240.00"),
        Decimal("0.00"),
    )

    # An invoice outside the earmark targets cannot draw the held cash.
    with _tenant_session(database_url) as session:
        wrong = issue_rated_invoice(
            session,
            scope=SCOPE,
            command=_issue_command(
                account_id, suffix="wrong", source_fact_id=f"occ-x-{uuid4()}"
            ),
            obligation_sources=_Sources(),
            numbering=_Numbering(),
        )
        with pytest.raises(BillingRuleViolation):
            allocate_settlement(
                session,
                scope=SCOPE,
                command=AllocationCommand(
                    settlement_id=settlement_id,
                    document_id=wrong.invoice_id,
                    amount=_money("120.00"),
                    occurred_at=NOW,
                    source_ref="prepaid-proof:wrong-target",
                ),
            )
        session.rollback()

    # Each period invoice draws exactly its own earmarked target.
    for index, fact in enumerate(period_facts):
        with _tenant_session(database_url) as session:
            issued = issue_rated_invoice(
                session,
                scope=SCOPE,
                command=_issue_command(
                    account_id, suffix=f"p{index + 1}", source_fact_id=fact
                ),
                obligation_sources=_Sources(),
                numbering=_Numbering(),
            )
            allocate_settlement(
                session,
                scope=SCOPE,
                command=AllocationCommand(
                    settlement_id=settlement_id,
                    document_id=issued.invoice_id,
                    amount=_money("120.00"),
                    occurred_at=NOW,
                    source_ref=f"prepaid-proof:period-{index + 1}",
                ),
            )
            session.commit()
        remaining = Decimal("120.00") if index == 0 else Decimal("0.00")
        assert _availability(database_url, account_id, settlement_id) == (
            remaining,
            remaining,
            Decimal("0.00"),
        )
    assert _lanes(database_url, account_id)[0] == Decimal("0.00")
