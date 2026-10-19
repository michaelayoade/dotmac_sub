"""Census preview -> rehearsal census wire rendering is field-exact.

The wire document is the exact JSON Starter's read-only rehearsal adapter
consumes: dataclass field names carry across verbatim, Decimals render as
exact plain strings (never JSON numbers or exponents), datetimes render
as aware UTC ISO instants, enums as their values, and the v3 unavailable-
original-evidence records ride along for the adapter's sidecar. Rendering
fails closed on naive instants, non-finite money, non-wire populations,
and non-v3 previews.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.billing import InvoiceStatus, PaymentStatus
from app.models.billing_contract import BillingRecordAuthority
from app.services.billing.opening_census_wire import (
    OpeningCensusWireError,
    render_opening_census_wire,
)
from app.services.billing.opening_export_contracts import (
    BillingOpeningExportPreviewV3,
    CaptureEvidence,
    CashEvidencePopulation,
    CashPaymentEvidence,
    CashSourceCount,
    CashSourceKind,
    DocumentaryObservationPopulation,
    ExceptionCode,
    InvoiceOpeningEvidence,
    LaneEvidence,
    OpeningControl,
    OpeningException,
    OriginalEvidenceRequirement,
    OriginalEvidenceUnavailableReason,
    PreviewBillingOpeningExportQuery,
    SettlementEvidence,
    SettlementKind,
    UnavailableOriginalInvoiceEvidence,
)

_NOW = datetime(2026, 10, 18, tzinfo=UTC)


def _preview() -> BillingOpeningExportPreviewV3:
    account_id = uuid4()
    invoice_id = uuid4()
    settlement_id = uuid4()
    payment_id = uuid4()
    query = PreviewBillingOpeningExportQuery(
        source_repository_sha="abc123",
        installation_id=uuid4(),
        economic_cutoff=_NOW,
        currency_precisions=(("NGN", 2),),
        population_version="sub-billing-opening-census-v3",
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
    exception = OpeningException(
        code=ExceptionCode.cash_unqualified,
        account_id=account_id,
        currency="NGN",
        source_id=payment_id,
        detail="no verifier",
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
        source_counts=tuple(
            CashSourceCount(
                table=kind, count=1 if kind is CashSourceKind.payments else 0
            )
            for kind in CashSourceKind
        ),
        blocking_exceptions=(exception,),
    )
    return BillingOpeningExportPreviewV3(
        capture=CaptureEvidence(
            declared_source=query,
            schema_heads=("692_starter_invoice_line_links",),
            snapshot_id="snapshot-1",
            captured_at=_NOW,
            isolation="repeatable read",
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
        blocking_exceptions=(),
        controls=(
            OpeningControl(
                account_id=account_id,
                currency="NGN",
                invoice_count=1,
                eligible_count=1,
                excluded_count=0,
                exception_count=1,
                original_total=Decimal("107.50"),
                canonical_discharge=Decimal("50.00"),
                residual=Decimal("57.50"),
                eligible_residual=Decimal("57.50"),
                source_count=1,
            ),
        ),
        content_fingerprint="census-fp",
        cash_evidence=cash,
        documentary_observations=DocumentaryObservationPopulation(
            current_invoices=(),
            current_lines=(),
            unavailable_original_evidence=(
                UnavailableOriginalInvoiceEvidence(
                    invoice_id=invoice_id,
                    requirements=(
                        OriginalEvidenceRequirement.documentary_digest,
                        OriginalEvidenceRequirement.seller,
                    ),
                    reasons=(
                        OriginalEvidenceUnavailableReason.no_authenticated_frozen_document,
                    ),
                ),
            ),
        ),
    )


def test_wire_maps_census_field_for_field() -> None:
    preview = _preview()
    payload = json.loads(render_opening_census_wire(preview))

    capture = payload["capture"]
    declared = capture["declared_source"]
    assert declared["population_version"] == "sub-billing-opening-census-v3"
    assert declared["currency_precisions"] == [["NGN", 2]]
    assert declared["economic_cutoff"] == "2026-10-18T00:00:00+00:00"
    assert capture["schema_heads"] == ["692_starter_invoice_line_links"]
    assert payload["content_fingerprint"] == "census-fp"

    (invoice,) = payload["invoices"]
    assert invoice["invoice_id"] == str(preview.invoices[0].invoice_id)
    assert invoice["status"] == "issued"
    assert invoice["total"] == "107.50"
    assert invoice["canonical_discharge"] == "50.00"
    assert invoice["residual"] == "57.50"
    assert invoice["settlement_ids"] == [str(preview.settlements[0].id)]

    (settlement,) = payload["settlements"]
    assert settlement["kind"] == "payment"
    assert settlement["amount"] == "50.00"

    (lane,) = payload["lanes"]
    assert lane["authority"] == "authoritative"
    assert lane["collectible_receivable"] == "57.50"

    cash = payload["cash_evidence"]
    counts = {row["table"]: row["count"] for row in cash["source_counts"]}
    assert set(counts) == {str(kind) for kind in CashSourceKind}
    assert counts["payments"] == 1
    (payment,) = cash["payments"]
    assert payment["id"] == str(preview.settlements[0].source_id)
    assert payment["amount"] == "50.00"
    assert payment["status"] == "succeeded"
    assert cash["qualified_payment_ids"] == []
    (cash_exception,) = cash["blocking_exceptions"]
    assert cash_exception["code"] == "cash_unqualified"

    (unavailable,) = payload["documentary_observations"][
        "unavailable_original_evidence"
    ]
    assert unavailable["requirements"] == ["documentary_digest", "seller"]
    assert unavailable["reasons"] == ["no_authenticated_frozen_document"]


def test_wire_refuses_naive_instant() -> None:
    preview = _preview()
    naive = replace(preview.invoices[0], issued_at=datetime(2026, 10, 18))
    with pytest.raises(OpeningCensusWireError):
        render_opening_census_wire(replace(preview, invoices=(naive,)))


def test_wire_refuses_nonfinite_money() -> None:
    preview = _preview()
    infinite = replace(preview.invoices[0], total=Decimal("Infinity"))
    with pytest.raises(OpeningCensusWireError):
        render_opening_census_wire(replace(preview, invoices=(infinite,)))


def test_wire_refuses_non_wire_population_version() -> None:
    preview = _preview()
    query = replace(
        preview.capture.declared_source,
        population_version="sub-billing-opening-census-v1",
    )
    capture = replace(preview.capture, declared_source=query)
    with pytest.raises(OpeningCensusWireError):
        render_opening_census_wire(replace(preview, capture=capture))
