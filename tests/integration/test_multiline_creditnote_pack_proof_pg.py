"""Multi-line issuance + credit-note proof for the pinned Starter pack.

Companion to the Step-3 money-path proof: the single-line rated-invoice
command cannot express Sub's multi-line admin invoices or corrections, so
this proves the composition the issuance lane will actually use —
``accept_rated_obligation`` per line, ``create_multi_line_draft_document``,
``issue_document`` with real numbering — and the correction path,
``issue_credit_note``, including its over-credit refusal. Positions are
asserted through the current receivable position reader at each step.

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
    AppliedFxSnapshotV1,
    AppliedTaxSnapshotV1,
    DueDateBasisV1,
    GetCurrentReceivablePositionV1,
    PartyDocumentSnapshotV1,
    PartyTaxIdentitySnapshotV1,
    PaymentInstructionsSnapshotV1,
    PostalAddressSnapshotV1,
    PresentationAssetReferenceV1,
    ServicePeriodEvidenceV1,
    get_current_receivable_position,
)
from dotmac_billing.commands import (
    CreateMultiLineDraftDocument,
    DraftDocumentLine,
    IssueCreditNote,
    IssueDocument,
)
from dotmac_billing.contracts import DueDateBasisStatus, ServicePeriodStatus
from dotmac_billing.errors import BillingRuleViolation
from dotmac_billing.service import (
    accept_rated_obligation,
    create_billing_account,
    create_multi_line_draft_document,
    issue_credit_note,
    issue_document,
)
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency, Money
from dotmac_numbering import SeriesConfiguration, allocate, configure_series
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


def _config(database_url: URL) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option(
        "sqlalchemy.url", database_url.render_as_string(hide_password=False)
    )
    return config


def _money(amount: str) -> Money:
    return Money.of(amount, Currency("NGN", 2))


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


def _basis() -> DueDateBasisV1:
    return DueDateBasisV1(
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


def _tax_snapshot(basis: str, tax: str) -> AppliedTaxSnapshotV1:
    return AppliedTaxSnapshotV1(
        treatment_code="standard",
        jurisdiction_code="source-supplied-zone",
        policy_id="tax-policy",
        policy_version="1",
        rate=Decimal("0.200000"),
        taxable_basis=_money(basis),
        tax_amount=_money(tax),
    )


def _obligation(account_id: UUID, *, suffix: str) -> AcceptRatedObligationV1:
    return AcceptRatedObligationV1(
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
        tax_snapshots=(_tax_snapshot("100.00", "20.00"),),
        fx_snapshot=AppliedFxSnapshotV1(
            observation_id=f"fx-{suffix}",
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
    )


def _party(name: str, line_one: str) -> PartyDocumentSnapshotV1:
    return PartyDocumentSnapshotV1(
        legal_name=name,
        address=PostalAddressSnapshotV1(
            line_one=line_one, city="Abuja", country_code="NGA"
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


def test_multiline_issue_then_credit_note(isolated_database: URL) -> None:
    database_url = isolated_database
    command.upgrade(_config(database_url), "heads")

    with _tenant_session(database_url) as session:
        for series in ("invoice", "credit-note"):
            configure_series(
                session,
                scope=SCOPE,
                configuration=SeriesConfiguration(
                    series_code=series, prefix=series.upper(), min_digits=6
                ),
            )
        account = create_billing_account(
            session,
            scope=SCOPE,
            external_account_ref=f"multiline-{uuid4()}",
            currency="NGN",
            minor_units=2,
        )
        account_id = account.id
        obligations = [
            accept_rated_obligation(
                session,
                scope=SCOPE,
                command=_obligation(account_id, suffix=str(index)),
                accepted_source_kinds=frozenset({"recurring_charge_occurrence"}),
            )
            for index in (1, 2)
        ]
        draft = create_multi_line_draft_document(
            session,
            scope=SCOPE,
            command=CreateMultiLineDraftDocument(
                billing_account_id=account_id,
                idempotency_key=f"multi:{obligations[0].id}",
                lines=tuple(
                    DraftDocumentLine(
                        obligation_id=row.id,
                        description=f"Access {index}",
                        quantity=Decimal("2"),
                        unit_code="month",
                        unit_amount=_money("50.00"),
                    )
                    for index, row in enumerate(obligations)
                ),
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
                due_date_basis=_basis(),
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
            ),
        )
        issued = issue_document(
            session,
            scope=SCOPE,
            command=IssueDocument(
                document_id=draft.id,
                series_code="invoice",
                reference_date=date(2026, 10, 17),
                due_at=NOW + timedelta(days=30),
                due_date_basis=_basis(),
                actor_ref="multiline-proof",
                correlation_id="multiline-proof",
            ),
            numbering=_Numbering(),
        )
        document_id = issued.id
        session.commit()
    # Two lines of 100.00 pre-tax + 20.00 tax each.
    assert _collectible(database_url, account_id) == Decimal("240.00")

    # Over-credit is refused before any correction posts.
    with _tenant_session(database_url) as session:
        with pytest.raises(BillingRuleViolation):
            issue_credit_note(
                session,
                scope=SCOPE,
                command=IssueCreditNote(
                    original_document_id=document_id,
                    pre_tax_amount=_money("250.00"),
                    tax_amount=_money("50.00"),
                    total_amount=_money("300.00"),
                    reason="over-credit attempt",
                    series_code="credit-note",
                    reference_date=date(2026, 10, 17),
                    actor_ref="multiline-proof",
                    correlation_id="multiline-proof-cn-over",
                    occurred_at=NOW,
                    document_profile_code="credit_note.standard",
                    document_profile_version="1",
                    tax_snapshots=(_tax_snapshot("250.00", "50.00"),),
                ),
                numbering=_Numbering(),
            )
        session.rollback()

    # A partial credit note reduces the collectible position exactly.
    with _tenant_session(database_url) as session:
        issue_credit_note(
            session,
            scope=SCOPE,
            command=IssueCreditNote(
                original_document_id=document_id,
                pre_tax_amount=_money("50.00"),
                tax_amount=_money("10.00"),
                total_amount=_money("60.00"),
                reason="goodwill adjustment",
                series_code="credit-note",
                reference_date=date(2026, 10, 17),
                actor_ref="multiline-proof",
                correlation_id="multiline-proof-cn",
                occurred_at=NOW,
                document_profile_code="credit_note.standard",
                document_profile_version="1",
                tax_snapshots=(_tax_snapshot("50.00", "10.00"),),
            ),
            numbering=_Numbering(),
        )
        session.commit()
    assert _collectible(database_url, account_id) == Decimal("180.00")
