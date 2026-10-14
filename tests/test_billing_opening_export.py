"""Evidence classification tests; migrated PostgreSQL owns database acceptance."""

import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from unittest.mock import MagicMock, Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.models.billing import InvoiceStatus, TaxApplication
from app.models.billing_contract import BillingRecordAuthority
from app.services.billing import opening_export as owner
from app.services.billing.customer_subledger import (
    CustomerFinancialPosition,
    CustomerSubledgerPositionEvidence,
)
from app.services.billing.opening_export_contracts import (
    BillingOpeningExportPreview,
    BillingOpeningExportPreviewV2,
    BillingOpeningExportPreviewV3,
    CaptureEvidence,
    CashEvidencePopulation,
    DocumentaryObservationPopulation,
    ExceptionCode,
    ExclusionReason,
    FinancialSourceEvidence,
    InvoiceOpeningEvidence,
    OriginalEvidenceRequirement,
    OriginalEvidenceUnavailableReason,
    PreviewBillingOpeningExportQuery,
    SettlementEvidence,
    SettlementKind,
    SourceKind,
    UnavailableOriginalInvoiceEvidence,
)
from scripts.migration import billing_opening_export as cli
from scripts.migration.billing_opening_export import _json_value

AT = datetime(2026, 10, 13, tzinfo=UTC)
ACCOUNT = UUID(int=1)
QUERY = PreviewBillingOpeningExportQuery(
    "a" * 40, UUID(int=2), AT, (("NGN", 2), ("USD", 2))
)
CAPTURE = CaptureEvidence(
    QUERY, ("schema-head",), "snapshot", AT, "repeatable read", True
)


def invoice(*, currency: str = "NGN", residual: str = "100") -> InvoiceOpeningEvidence:
    amount = Decimal(residual)
    return InvoiceOpeningEvidence(
        invoice_id=uuid4(),
        account_id=ACCOUNT,
        currency=currency,
        precision=2,
        invoice_number=uuid4().hex,
        status=InvoiceStatus.issued,
        active=True,
        proforma=False,
        issued_at=AT - timedelta(days=900),
        due_at=AT,
        due_basis=None,
        due_basis_ref=None,
        due_policy_version=None,
        total=Decimal("100"),
        stored_residual=amount,
        payments_applied=Decimal("100") - amount,
        credits_applied=Decimal("0"),
        opening_funding_applied=Decimal("0"),
        canonical_discharge=Decimal("100") - amount,
        residual=amount,
        created_at=AT - timedelta(days=900),
        updated_at=AT,
        collections_due_basis_verified=False,
        line_ids=(uuid4(),),
        settlement_ids=(),
    )


class PositionResolver(Protocol):
    def __call__(
        self,
        db: Session,
        *,
        account_id: UUID,
        currency: str,
        authority: BillingRecordAuthority,
        as_of: datetime,
    ) -> CustomerSubledgerPositionEvidence: ...


def resolve(*, ar: Decimal, credit: Decimal = Decimal("0")) -> PositionResolver:
    def callback(
        db: Session,
        *,
        account_id: UUID,
        currency: str,
        authority: BillingRecordAuthority,
        as_of: datetime,
    ) -> CustomerSubledgerPositionEvidence:
        assert as_of == AT
        return CustomerSubledgerPositionEvidence(
            CustomerFinancialPosition(
                account_id,
                currency,
                authority,
                ar,
                credit,
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
            ),
            "b" * 64,
            1,
        )

    return callback


def classify(
    monkeypatch: pytest.MonkeyPatch,
    items: tuple[InvoiceOpeningEvidence, ...],
    *,
    cutover: UUID | None = UUID(int=3),
    sources: tuple[FinancialSourceEvidence, ...] = (),
) -> BillingOpeningExportPreview:
    monkeypatch.setattr(
        owner,
        "resolve_position_evidence_at",
        resolve(
            ar=sum(
                (i.residual for i in items if owner._exclusion(i) is None), Decimal("0")
            )
        ),
    )
    return owner._classify(
        Mock(spec=Session),
        QUERY,
        CAPTURE,
        (ACCOUNT,),
        items,
        (),
        (),
        sources,
        (),
        cutover,
    )


def test_old_unlinked_and_unknown_due_items_are_counted_without_collections_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = invoice(), invoice(residual="25")
    result = classify(monkeypatch, (first, second))
    assert result.eligible_invoice_ids == (first.invoice_id, second.invoice_id)
    assert all(not i.collections_due_basis_verified for i in result.invoices)
    assert result.capture.population_complete
    control = result.controls[0]
    assert (control.invoice_count, control.eligible_count) == (2, 2)
    assert control.original_total == Decimal("200")
    assert control.canonical_discharge == Decimal("75")
    assert control.residual == control.eligible_residual == Decimal("125")


def test_shadow_authority_remains_comparison_and_blocks_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = classify(monkeypatch, (invoice(),), cutover=None)
    assert not result.eligible_invoice_ids
    assert result.lanes[0].authority == BillingRecordAuthority.shadow
    assert ExceptionCode.source_authority in {
        e.code for e in result.blocking_exceptions
    }
    assert sum(c.exception_count for c in result.controls) == len(
        result.blocking_exceptions
    )


def test_negative_mismatch_and_excluded_residue_are_not_clamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    negative = replace(invoice(residual="-10"), stored_residual=Decimal("0"))
    closed = replace(invoice(), status=InvoiceStatus.paid)
    result = classify(monkeypatch, (negative, closed))
    assert result.invoices[0].residual == Decimal("-10")
    assert not result.eligible_invoice_ids
    assert {
        ExceptionCode.negative_residual,
        ExceptionCode.residual_mismatch,
        ExceptionCode.excluded_nonzero_residue,
    } <= {e.code for e in result.blocking_exceptions}
    assert result.exclusions[0].reason == ExclusionReason.fully_closed
    assert result.controls[0].residual == Decimal("90")


def test_mutable_source_after_cutoff_blocks_without_filtering_population(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = replace(invoice(), updated_at=AT + timedelta(seconds=1))
    result = classify(monkeypatch, (item,))
    assert result.invoices == (item,)
    assert not result.eligible_invoice_ids
    assert any(
        e.code == ExceptionCode.source_after_cutoff for e in result.blocking_exceptions
    )


def test_orphan_financial_source_is_counted_and_never_mapped_from_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = FinancialSourceEvidence(
        SourceKind.payment,
        uuid4(),
        None,
        "NGN",
        Decimal("40"),
        Decimal("0"),
        "succeeded",
        True,
        None,
        None,
        None,
        None,
        AT,
        AT,
        AT,
    )
    result = classify(monkeypatch, (), sources=(source,))
    assert result.financial_sources == (source,)
    assert any(
        e.code == ExceptionCode.account_mapping for e in result.blocking_exceptions
    )
    assert result.controls[0].source_count == 1
    assert result.controls[0].exception_count == len(result.blocking_exceptions)
    assert result.exclusions[0].reason == ExclusionReason.no_financial_activity


def test_nonzero_credit_lane_is_not_netted_against_ar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = invoice()
    monkeypatch.setattr(
        owner,
        "resolve_position_evidence_at",
        resolve(ar=Decimal("100"), credit=Decimal("100")),
    )
    result = owner._classify(
        Mock(spec=Session),
        QUERY,
        CAPTURE,
        (ACCOUNT,),
        (item,),
        (),
        (),
        (),
        (),
        UUID(int=3),
    )
    assert (
        result.lanes[0].collectible_receivable
        == result.lanes[0].unapplied_customer_credit
        == Decimal("100")
    )
    assert result.controls[0].residual == Decimal("100")
    assert not result.eligible_invoice_ids
    assert any(
        e.code == ExceptionCode.unbacked_lane for e in result.blocking_exceptions
    )


def test_fingerprint_ignores_capture_clock_but_detects_source_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = invoice()
    result = classify(monkeypatch, (first,))
    later = replace(
        result,
        capture=replace(
            CAPTURE, snapshot_id="different", captured_at=AT + timedelta(seconds=1)
        ),
    )
    assert owner._fingerprint(later) == result.content_fingerprint
    assert (
        owner._fingerprint(
            replace(result, invoices=(replace(first, invoice_number="changed"),))
        )
        != result.content_fingerprint
    )
    assert (
        owner._fingerprint(
            replace(result, capture=replace(CAPTURE, schema_heads=("other",)))
        )
        != result.content_fingerprint
    )


def test_dirty_snapshot_fails_before_database_access_and_preserves_autoflush() -> None:
    db = Mock(spec=Session)
    db.new, db.dirty, db.deleted = {object()}, set(), set()
    with pytest.raises(owner.BillingOpeningExportError) as error:
        owner._capture(db, QUERY)
    assert error.value.code.endswith("snapshot_required")
    db.execute.assert_not_called()


def test_currencies_keep_separate_controls_and_never_net(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ngn = invoice(currency="NGN", residual="100")
    usd = invoice(currency="USD", residual="-100")

    def separate(
        db: Session,
        *,
        account_id: UUID,
        currency: str,
        authority: BillingRecordAuthority,
        as_of: datetime,
    ) -> CustomerSubledgerPositionEvidence:
        return resolve(ar=Decimal("100") if currency == "NGN" else Decimal("-100"))(
            db,
            account_id=account_id,
            currency=currency,
            authority=authority,
            as_of=as_of,
        )

    monkeypatch.setattr(owner, "resolve_position_evidence_at", separate)
    result = owner._classify(
        Mock(spec=Session),
        QUERY,
        CAPTURE,
        (ACCOUNT,),
        (ngn, usd),
        (),
        (),
        (),
        (),
        UUID(int=3),
    )
    assert result.eligible_invoice_ids == (ngn.invoice_id,)
    assert {c.currency: c.residual for c in result.controls} == {
        "NGN": Decimal("100"),
        "USD": Decimal("-100"),
    }
    assert all(e.currency == "USD" for e in result.blocking_exceptions)


def ledger(
    *,
    item: InvoiceOpeningEvidence,
    amount: str,
    direction: str = "credit",
    source: str = "credit_note",
    invoice_id: UUID | None = None,
    payment_id: UUID | None = None,
    reverses_id: UUID | None = None,
    active: bool = True,
) -> FinancialSourceEvidence:
    return FinancialSourceEvidence(
        SourceKind.ledger,
        uuid4(),
        item.account_id,
        item.currency,
        Decimal(amount),
        None,
        direction,
        active,
        invoice_id,
        payment_id,
        None,
        reverses_id,
        AT,
        AT,
        AT,
        source_kind=source,
    )


def test_multiple_credit_applications_and_linked_negative_reversal_are_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = invoice(residual="70")
    funding = ledger(item=item, amount="100")
    first = ledger(item=item, amount="20", invoice_id=item.invoice_id)
    second = ledger(item=item, amount="30", invoice_id=item.invoice_id)
    reversal = ledger(
        item=item,
        amount="20",
        direction="debit",
        invoice_id=item.invoice_id,
        reverses_id=first.id,
    )
    note = FinancialSourceEvidence(
        SourceKind.credit_note,
        uuid4(),
        ACCOUNT,
        "NGN",
        Decimal("100"),
        Decimal("30"),
        "issued",
        True,
        None,
        None,
        funding.id,
        None,
        AT,
        AT,
        AT,
    )
    movements = tuple(
        SettlementEvidence(
            SettlementKind.credit_note,
            uuid4(),
            item.invoice_id,
            note.id,
            amount,
            entry.id,
            None,
            True,
            AT,
        )
        for amount, entry in (
            (Decimal("20"), first),
            (Decimal("30"), second),
            (Decimal("-20"), reversal),
        )
    )
    monkeypatch.setattr(
        owner, "resolve_position_evidence_at", resolve(ar=Decimal("70"))
    )
    result = owner._classify(
        Mock(spec=Session),
        QUERY,
        CAPTURE,
        (ACCOUNT,),
        (item,),
        (),
        movements,
        (note, funding, first, second, reversal),
        (),
        UUID(int=3),
    )
    assert not result.blocking_exceptions
    assert result.eligible_invoice_ids == (item.invoice_id,)
    assert len(result.settlements) == 3


@pytest.mark.parametrize(
    "wrong", ["invoice", "payment", "direction", "source", "amount", "inactive"]
)
def test_same_account_currency_ledger_is_insufficient_without_exact_affinity(
    monkeypatch: pytest.MonkeyPatch, wrong: str
) -> None:
    item = invoice(residual="50")
    payment_id = uuid4()
    payment = FinancialSourceEvidence(
        SourceKind.payment,
        payment_id,
        ACCOUNT,
        "NGN",
        Decimal("50"),
        Decimal("0"),
        "succeeded",
        True,
        None,
        payment_id,
        None,
        None,
        AT,
        AT,
        AT,
    )
    entry = ledger(
        item=item,
        amount="50",
        source="payment",
        invoice_id=item.invoice_id,
        payment_id=payment_id,
    )
    if wrong == "invoice":
        entry = replace(entry, invoice_id=uuid4())
    elif wrong == "payment":
        entry = replace(entry, payment_id=uuid4())
    elif wrong == "direction":
        entry = replace(entry, status="debit")
    elif wrong == "source":
        entry = replace(entry, source_kind="adjustment")
    elif wrong == "amount":
        entry = replace(entry, amount=Decimal("49"))
    elif wrong == "inactive":
        entry = replace(entry, active=False)
    settlement = SettlementEvidence(
        SettlementKind.payment,
        uuid4(),
        item.invoice_id,
        payment_id,
        Decimal("50"),
        entry.id,
        None,
        True,
        AT,
    )
    monkeypatch.setattr(
        owner, "resolve_position_evidence_at", resolve(ar=Decimal("50"))
    )
    result = owner._classify(
        Mock(spec=Session),
        QUERY,
        CAPTURE,
        (ACCOUNT,),
        (item,),
        (),
        (settlement,),
        (payment, entry),
        (),
        UUID(int=3),
    )
    assert not result.eligible_invoice_ids
    assert any(
        e.code == ExceptionCode.settlement_provenance
        for e in result.blocking_exceptions
    )


@pytest.mark.parametrize("nonfinite", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_source_fails_capture_with_typed_error_without_finite_substitute(
    monkeypatch: pytest.MonkeyPatch, nonfinite: str
) -> None:
    item = replace(invoice(), residual=Decimal(nonfinite))
    with pytest.raises(owner.BillingOpeningExportError) as error:
        owner._classify(
            Mock(spec=Session),
            QUERY,
            CAPTURE,
            (ACCOUNT,),
            (item,),
            (),
            (),
            (),
            (),
            UUID(int=3),
        )
    assert error.value.code.endswith("nonfinite_source")


def test_credit_reversal_without_exact_original_link_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = invoice()
    entry = ledger(
        item=item, amount="20", direction="debit", invoice_id=item.invoice_id
    )
    note = FinancialSourceEvidence(
        SourceKind.credit_note,
        uuid4(),
        ACCOUNT,
        "NGN",
        Decimal("0"),
        Decimal("0"),
        "issued",
        True,
        None,
        None,
        None,
        None,
        AT,
        AT,
        AT,
    )
    movement = SettlementEvidence(
        SettlementKind.credit_note,
        uuid4(),
        item.invoice_id,
        note.id,
        Decimal("-20"),
        entry.id,
        None,
        True,
        AT,
    )
    monkeypatch.setattr(
        owner, "resolve_position_evidence_at", resolve(ar=Decimal("100"))
    )
    result = owner._classify(
        Mock(spec=Session),
        QUERY,
        CAPTURE,
        (ACCOUNT,),
        (item,),
        (),
        (movement,),
        (note, entry),
        (),
        UUID(int=3),
    )
    assert any(
        e.code == ExceptionCode.settlement_provenance
        for e in result.blocking_exceptions
    )


@pytest.mark.parametrize(
    "version,expected_fingerprint,expected_wire_digest",
    [
        (
            1,
            "48ebabf8ef6ba4fca8c60ab927c70ab0f460a578f32f405f7628bd6d1ff36a89",
            "5970d2b58b66b473fc08ce9239385d4af948a0d9d221070b17f1406bb1fc4e58",
        ),
        (
            2,
            "046071c89ea325e150ef56683e16bbfdc0fa6c3ef4d6a68d92793c3336c77b67",
            "7d3658774a1c4347b9823967a414396b8993417fcc84134e5aaf87cba21f8a41",
        ),
    ],
)
def test_v1_v2_exact_fingerprint_and_cli_bytes_are_pinned(
    version: int, expected_fingerprint: str, expected_wire_digest: str
) -> None:
    # Fixed empty-population vectors pin the complete inherited envelope too.
    capture = replace(
        CAPTURE,
        declared_source=replace(
            QUERY, population_version=f"sub-billing-opening-census-v{version}"
        ),
    )
    report = BillingOpeningExportPreview(
        capture, (), (), (), (), (), (), (), (), (), (), (), ""
    )
    if version == 2:
        report = BillingOpeningExportPreviewV2(
            capture,
            (),
            (),
            (),
            (),
            (),
            (),
            (),
            (),
            (),
            (),
            (),
            "",
            CashEvidencePopulation((), (), (), (), (), (), (), (), (), (), ()),
        )
    assert owner._fingerprint(report) == expected_fingerprint
    report = replace(report, content_fingerprint=expected_fingerprint)
    wire = (
        json.dumps(_json_value(asdict(report)), sort_keys=True, allow_nan=False) + "\n"
    )
    assert hashlib.sha256(wire.encode()).hexdigest() == expected_wire_digest
    assert "documentary_observations" not in wire


@pytest.mark.parametrize("percent", [None, Decimal("0"), Decimal("0.0750")])
def test_v3_current_tax_observations_preserve_null_and_zero_without_qualification(
    monkeypatch: pytest.MonkeyPatch, percent: Decimal | None
) -> None:
    item = replace(invoice(), invoice_id=UUID(int=11))
    base = classify(monkeypatch, (item,))
    tax_id = UUID(int=13) if percent is not None else None
    db = Mock(spec=Session)
    db.execute.side_effect = [
        [
            (
                item.invoice_id,
                Decimal("95"),
                Decimal("5"),
                Decimal("10"),
                Decimal("100"),
                None,
                None,
            )
        ],
        [
            (
                UUID(int=12),
                item.invoice_id,
                "Current description",
                tax_id,
                1 if percent is not None else None,
                None,
                percent,
                False if percent is not None else None,
                TaxApplication.exclusive,
            )
        ],
    ]
    observations = owner._documentary_observations(db, report=base)
    assert observations.current_invoices[0].subtotal == Decimal("95")
    assert observations.current_lines[0].tax_rate_percent_snapshot == percent
    assert observations.current_lines[0].tax_rate_id == tax_id
    gap = observations.unavailable_original_evidence[0]
    assert gap.invoice_id == item.invoice_id
    assert set(gap.requirements) == set(OriginalEvidenceRequirement)
    assert OriginalEvidenceRequirement.documentary_digest in gap.requirements
    assert (
        OriginalEvidenceRequirement.withholding_non_cash_replay_bindings
        in gap.requirements
    )
    report = BillingOpeningExportPreviewV3(
        replace(
            CAPTURE,
            declared_source=replace(
                QUERY, population_version="sub-billing-opening-census-v3"
            ),
        ),
        base.subscriber_ids,
        base.invoices,
        base.lines,
        base.settlements,
        base.financial_sources,
        base.lanes,
        base.effects,
        base.eligible_invoice_ids,
        base.exclusions,
        base.blocking_exceptions,
        base.controls,
        "",
        CashEvidencePopulation((), (), (), (), (), (), (), (), (), (), ()),
        observations,
    )
    drift = replace(
        report,
        documentary_observations=replace(
            observations,
            current_lines=(
                replace(observations.current_lines[0], description="Changed"),
            ),
        ),
    )
    assert owner._fingerprint(report) != owner._fingerprint(drift)
    assert report.eligible_invoice_ids == base.eligible_invoice_ids
    assert report.cash_evidence.qualified_payment_ids == ()


def test_v3_nonfinite_current_document_observation_fails_without_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = invoice()
    base = classify(monkeypatch, (item,))
    db = Mock(spec=Session)
    db.execute.side_effect = [
        [
            (
                item.invoice_id,
                Decimal("NaN"),
                Decimal("0"),
                Decimal("0"),
                Decimal("100"),
                None,
                None,
            )
        ],
        [],
    ]
    with pytest.raises(owner.BillingOpeningExportError) as error:
        owner._documentary_observations(db, report=base)
    assert error.value.code.endswith("nonfinite_source")


def test_v3_cli_reports_unavailable_original_evidence_as_blocking(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    query = replace(QUERY, population_version="sub-billing-opening-census-v3")
    report = BillingOpeningExportPreviewV3(
        replace(CAPTURE, declared_source=query),
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        "",
        CashEvidencePopulation((), (), (), (), (), (), (), (), (), (), ()),
        DocumentaryObservationPopulation(
            (),
            (),
            (
                UnavailableOriginalInvoiceEvidence(
                    UUID(int=11),
                    tuple(OriginalEvidenceRequirement),
                    tuple(OriginalEvidenceUnavailableReason),
                ),
            ),
        ),
    )
    context = MagicMock()
    monkeypatch.setattr(cli, "read_only_snapshot_session", Mock(return_value=context))
    census = Mock(return_value=report)
    monkeypatch.setattr(cli, "preview_billing_opening_export", census)
    assert (
        cli.main(
            [
                "--source-repository-sha",
                query.source_repository_sha,
                "--installation-id",
                str(query.installation_id),
                "--economic-cutoff",
                AT.isoformat(),
                "--currency-precision",
                "NGN:2",
                "--currency-precision",
                "USD:2",
                "--population-version",
                query.population_version,
            ]
        )
        == 1
    )
    census.assert_called_once_with(context.__enter__.return_value, query=query)
    output = json.loads(capsys.readouterr().out)
    assert (
        output["capture"]["declared_source"]["population_version"]
        == query.population_version
    )
    gap = output["documentary_observations"]["unavailable_original_evidence"][0]
    assert gap["invoice_id"] == str(UUID(int=11))
    assert "documentary_digest" in gap["requirements"]
    assert gap["reasons"] == [
        reason.value for reason in OriginalEvidenceUnavailableReason
    ]
