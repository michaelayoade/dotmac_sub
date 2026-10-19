"""Census preview → rehearsal observation mapping is field-exact.

The builder translates evidence only: invoice/settlement/control/lane
rows carry across 1:1, cash populations keep their canonical table
names and sorted identities, and blocking exceptions from both the
receivable and cash censuses group into counted blockers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.models.billing import InvoiceStatus, PaymentStatus
from app.models.billing_contract import BillingRecordAuthority
from app.services.billing.opening_export_contracts import (
    BillingOpeningExportPreviewV2,
    CaptureEvidence,
    CashEvidencePopulation,
    CashPaymentEvidence,
    CashSourceCount,
    CashSourceKind,
    ExceptionCode,
    InvoiceOpeningEvidence,
    LaneEvidence,
    OpeningControl,
    OpeningException,
    PreviewBillingOpeningExportQuery,
    SettlementEvidence,
    SettlementKind,
)
from app.services.billing.opening_rehearsal_assembly import (
    build_opening_sub_observation,
)

_NOW = datetime(2026, 10, 18, tzinfo=UTC)


def _preview() -> BillingOpeningExportPreviewV2:
    account_id = uuid4()
    invoice_id = uuid4()
    settlement_id = uuid4()
    payment_id = uuid4()
    query = PreviewBillingOpeningExportQuery(
        source_repository_sha="abc123",
        installation_id=uuid4(),
        economic_cutoff=_NOW,
        currency_precisions=(("NGN", 2),),
    )
    invoice = InvoiceOpeningEvidence(
        invoice_id=invoice_id,
        account_id=account_id,
        currency="NGN",
        precision=2,
        invoice_number="INV-1",
        status=InvoiceStatus.issued,
        active=True,
        proforma=False,
        issued_at=_NOW,
        due_at=_NOW,
        due_basis=None,
        due_basis_ref=None,
        due_policy_version=None,
        total=Decimal("107.50"),
        stored_residual=Decimal("57.50"),
        payments_applied=Decimal("50.00"),
        credits_applied=Decimal("0.00"),
        opening_funding_applied=Decimal("0.00"),
        canonical_discharge=Decimal("50.00"),
        residual=Decimal("57.50"),
        created_at=_NOW,
        updated_at=_NOW,
        collections_due_basis_verified=False,
        line_ids=(),
        settlement_ids=(settlement_id,),
    )
    settlement = SettlementEvidence(
        kind=SettlementKind.payment,
        id=settlement_id,
        invoice_id=invoice_id,
        source_id=payment_id,
        amount=Decimal("50.00"),
        ledger_entry_id=None,
        consumption_ledger_entry_id=None,
        active=True,
        created_at=_NOW,
    )
    payment = CashPaymentEvidence(
        id=payment_id,
        account_id=account_id,
        billing_account_id=None,
        provider_id=None,
        external_id="txn-1",
        amount=Decimal("50.00"),
        provider_fee=Decimal("0.00"),
        refunded_amount=Decimal("0.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        is_active=True,
        paid_at=_NOW,
        reserved_for_purchase_id=None,
        import_run_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    exceptions = (
        OpeningException(
            code=ExceptionCode.cash_unqualified,
            account_id=account_id,
            currency="NGN",
            source_id=payment_id,
            detail="no verifier",
        ),
        OpeningException(
            code=ExceptionCode.cash_unqualified,
            account_id=account_id,
            currency="NGN",
            source_id=settlement_id,
            detail="no verifier",
        ),
    )
    cash = CashEvidencePopulation(
        payments=(payment,),
        payment_settlements=(),
        provider_events=(),
        inbox_receipts=(),
        refunds=(),
        reversals=(),
        proofs=(),
        proof_corrections=(),
        topups=(),
        source_counts=(
            CashSourceCount(table=CashSourceKind.payments, count=1),
            CashSourceCount(table=CashSourceKind.payment_settlements, count=0),
            CashSourceCount(table=CashSourceKind.payment_provider_events, count=0),
            CashSourceCount(table=CashSourceKind.integration_inbox, count=0),
            CashSourceCount(table=CashSourceKind.payment_refunds, count=0),
            CashSourceCount(table=CashSourceKind.payment_reversals, count=0),
            CashSourceCount(table=CashSourceKind.payment_proofs, count=0),
            CashSourceCount(table=CashSourceKind.payment_proof_corrections, count=0),
            CashSourceCount(table=CashSourceKind.topup_intents, count=0),
        ),
        blocking_exceptions=(exceptions[0],),
    )
    return BillingOpeningExportPreviewV2(
        capture=CaptureEvidence(
            declared_source=query,
            schema_heads=("692_starter_invoice_line_links",),
            snapshot_id="snapshot-1",
            captured_at=_NOW,
            isolation="repeatable-read",
            population_complete=True,
        ),
        subscriber_ids=(account_id,),
        invoices=(invoice,),
        lines=(),
        settlements=(settlement,),
        financial_sources=(),
        lanes=(
            LaneEvidence(
                account_id=account_id,
                currency="NGN",
                authority=BillingRecordAuthority.authoritative,
                authority_cutover_id=None,
                fingerprint="lane-fp",
                posting_group_count=1,
                collectible_receivable=Decimal("57.50"),
                unapplied_customer_credit=Decimal("0.00"),
                prepaid_funding_reserved=Decimal("0.00"),
                prepaid_funding_consumed=Decimal("0.00"),
                written_off_total=Decimal("0.00"),
                refunded_total=Decimal("0.00"),
                adjustment_total=Decimal("0.00"),
            ),
        ),
        effects=(),
        eligible_invoice_ids=(invoice_id,),
        exclusions=(),
        blocking_exceptions=(exceptions[1],),
        controls=(
            OpeningControl(
                account_id=account_id,
                currency="NGN",
                invoice_count=1,
                eligible_count=1,
                excluded_count=0,
                exception_count=2,
                original_total=Decimal("107.50"),
                canonical_discharge=Decimal("50.00"),
                residual=Decimal("57.50"),
                eligible_residual=Decimal("57.50"),
                source_count=1,
            ),
        ),
        content_fingerprint="census-fp",
        cash_evidence=cash,
    )


def test_observation_maps_census_field_for_field() -> None:
    preview = _preview()
    observation = build_opening_sub_observation(preview)

    assert observation.source_repository_sha == "abc123"
    assert observation.schema_heads == ("692_starter_invoice_line_links",)
    assert observation.content_fingerprint == "census-fp"
    assert observation.currency_precisions[0].currency == "NGN"
    assert observation.currency_precisions[0].precision == 2
    assert observation.invoice_ids == (preview.invoices[0].invoice_id,)
    assert observation.eligible_invoice_ids == preview.eligible_invoice_ids

    (invoice,) = observation.invoices
    assert invoice.total == Decimal("107.50")
    assert invoice.canonical_discharge == Decimal("50.00")
    assert invoice.residual == Decimal("57.50")
    assert invoice.settlement_ids == preview.invoices[0].settlement_ids

    (settlement,) = observation.settlements
    assert settlement.kind == "payment"
    assert settlement.amount == Decimal("50.00")

    (lane,) = observation.lanes
    assert lane.authority == "authoritative"
    assert lane.collectible_receivable == Decimal("57.50")

    assert observation.qualified_payment_ids == ()
    populations = {row.table: row for row in observation.cash_populations}
    assert set(populations) == {str(kind) for kind in CashSourceKind}
    assert populations["payments"].count == 1
    assert populations["payments"].identities == (preview.settlements[0].source_id,)

    (blocker,) = observation.blockers
    assert blocker.code == "cash_unqualified"
    assert blocker.count == 2
    assert len(blocker.references) == 2
