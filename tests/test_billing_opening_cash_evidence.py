"""Raw receipt capture must never promote structural observations into cash trust."""

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, Mock
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from app.models.billing import (
    PaymentProviderEventFinancialEffect,
    PaymentProviderEventSource,
    PaymentProviderEventStatus,
    PaymentSettlementOrigin,
    PaymentStatus,
)
from app.services.billing import opening_export as owner
from app.services.billing.opening_export_contracts import (
    BillingOpeningExportPreview,
    CaptureEvidence,
    CashEvidencePopulation,
    CashInboxEvidence,
    CashPaymentEvidence,
    CashProviderEventEvidence,
    CashSettlementEvidence,
    ExceptionCode,
    PreviewBillingOpeningExportQuery,
)
from scripts.migration import billing_opening_export as cli

AT = datetime(2026, 10, 13, tzinfo=UTC)
ACCOUNT, PAYMENT, PROVIDER = UUID(int=1), UUID(int=2), UUID(int=3)
QUERY = PreviewBillingOpeningExportQuery(
    "a" * 40, UUID(int=4), AT, (("NGN", 2),), "sub-billing-opening-census-v2"
)


def report() -> BillingOpeningExportPreview:
    return BillingOpeningExportPreview(
        CaptureEvidence(QUERY, ("head",), "snapshot", AT, "repeatable read", True),
        (ACCOUNT,),
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
    )


def payment() -> CashPaymentEvidence:
    return CashPaymentEvidence(
        PAYMENT,
        ACCOUNT,
        None,
        PROVIDER,
        "charge-1",
        Decimal("100.00"),
        Decimal("2.00"),
        Decimal("0.00"),
        "NGN",
        PaymentStatus.succeeded,
        True,
        AT,
        None,
        None,
        AT,
        AT,
    )


def event() -> CashProviderEventEvidence:
    return CashProviderEventEvidence(
        UUID(int=5),
        PROVIDER,
        PAYMENT,
        None,
        "event-1",
        "charge.success",
        None,
        PaymentProviderEventSource.verified_webhook,
        PaymentProviderEventStatus.processed,
        PaymentStatus.succeeded,
        Decimal("100.00"),
        Decimal("2.00"),
        Decimal("98.00"),
        "NGN",
        "charge-1",
        PaymentProviderEventFinancialEffect.none,
        "d" * 64,
        None,
        AT,
        AT,
    )


def cash() -> CashEvidencePopulation:
    return CashEvidencePopulation(
        (payment(),), (), (event(),), (), (), (), (), (), (), (), ()
    )


def test_verified_stored_status_and_digest_do_not_qualify_cash_or_net_gross() -> None:
    settlement = CashSettlementEvidence(
        UUID(int=6),
        PAYMENT,
        PaymentSettlementOrigin.provider_event,
        Decimal("98.00"),
        Decimal("98.00"),
        Decimal("0.00"),
        "NGN",
        None,
        None,
        None,
        None,
        None,
        AT,
    )
    population = replace(cash(), payment_settlements=(settlement,))
    result = owner._classify_cash(population, query=QUERY, report=report())
    assert result.qualified_payment_ids == ()
    assert result.payments[0].amount == Decimal("100.00")
    assert result.payment_settlements[0].amount == Decimal("98.00")
    assert result.provider_events[0].net_amount == Decimal("98.00")
    assert (
        sum(
            e.code == ExceptionCode.cash_unqualified for e in result.blocking_exceptions
        )
        == 3
    )
    assert not any(
        e.code == ExceptionCode.cash_event_ambiguity for e in result.blocking_exceptions
    )


def test_orphans_multiple_events_and_cross_provider_affinity_remain_counted_blockers() -> (
    None
):
    second = replace(event(), id=UUID(int=7), provider_id=UUID(int=8))
    orphan = replace(event(), id=UUID(int=9), payment_id=None)
    population = replace(cash(), provider_events=(event(), second, orphan))
    result = owner._classify_cash(population, query=QUERY, report=report())
    assert result.provider_events == population.provider_events
    assert any(
        e.code == ExceptionCode.cash_event_ambiguity for e in result.blocking_exceptions
    )
    assert {
        e.source_id
        for e in result.blocking_exceptions
        if e.code == ExceptionCode.settlement_mapping
    } == {second.id, orphan.id}
    assert result.qualified_payment_ids == ()


def test_inbox_consequence_links_are_structural_and_never_namespace_mapping() -> None:
    receipt = CashInboxEvidence(
        UUID(int=10),
        UUID(int=11),
        UUID(int=12),
        "external-event-1",
        "charge.success",
        "e" * 64,
        "processed",
        event().id,
        PAYMENT,
        PROVIDER,
        True,
        None,
        AT,
        AT,
        AT,
        AT,
    )
    result = owner._classify_cash(
        replace(cash(), inbox_receipts=(receipt,)), query=QUERY, report=report()
    )
    assert any(
        e.source_id == receipt.id and e.code == ExceptionCode.cash_unqualified
        for e in result.blocking_exceptions
    )
    assert result.qualified_payment_ids == ()
    crossed = replace(receipt, consequence_payment_id=UUID(int=13))
    result = owner._classify_cash(
        replace(cash(), inbox_receipts=(crossed,)), query=QUERY, report=report()
    )
    assert any(
        e.source_id == receipt.id and e.code == ExceptionCode.settlement_mapping
        for e in result.blocking_exceptions
    )


@pytest.mark.parametrize(
    "amount", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")]
)
def test_nonfinite_cash_evidence_fails_capture_without_substitution(
    amount: Decimal,
) -> None:
    population = replace(
        cash(), provider_events=(replace(event(), provider_fee=amount),)
    )
    with pytest.raises(owner.BillingOpeningExportError) as error:
        owner._classify_cash(population, query=QUERY, report=report())
    assert error.value.code.endswith("nonfinite_source")


def test_cutoff_preserves_late_receipt_and_aware_time_requirement() -> None:
    late = replace(event(), processed_at=AT + timedelta(seconds=1))
    result = owner._classify_cash(
        replace(cash(), provider_events=(late,)), query=QUERY, report=report()
    )
    assert result.provider_events == (late,)
    assert any(
        e.source_id == late.id and e.code == ExceptionCode.source_after_cutoff
        for e in result.blocking_exceptions
    )
    with pytest.raises(owner.BillingOpeningExportError) as error:
        owner._classify_cash(
            replace(
                cash(),
                provider_events=(replace(late, received_at=AT.replace(tzinfo=None)),),
            ),
            query=QUERY,
            report=report(),
        )
    assert error.value.code.endswith("schema_evidence_missing")


@pytest.mark.parametrize(
    "value,valid",
    [(None, True), (str(PAYMENT), True), ("not-a-uuid", False), (7, False)],
)
def test_only_typed_consequence_ids_survive_projection(
    value: object, valid: bool
) -> None:
    identifier, actual = owner._cash_consequence_id(value)
    assert actual is valid
    assert identifier == (PAYMENT if value == str(PAYMENT) else None)


def test_v1_keeps_original_shape_and_never_queries_cash_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = replace(QUERY, population_version="sub-billing-opening-census-v1")
    db = MagicMock(spec=Session)
    db.scalars.return_value = ()
    db.execute.return_value = ()
    capture = replace(report().capture, declared_source=query)
    monkeypatch.setattr(owner, "_capture", Mock(return_value=capture))
    monkeypatch.setattr(owner, "authority_cutover", Mock(return_value=None))
    cash_query = Mock(side_effect=AssertionError("V1 queried cash evidence"))
    monkeypatch.setattr(owner, "_cash_evidence", cash_query)
    actual = owner.preview_billing_opening_export(db, query=query)
    expected = owner._classify(db, query, capture, (), (), (), (), (), (), None)
    assert asdict(actual) == asdict(expected)
    assert "cash_evidence" not in asdict(actual)
    cash_query.assert_not_called()


def test_v2_serialization_is_exact_and_distinct_from_v1() -> None:
    population = owner._classify_cash(cash(), query=QUERY, report=report())
    encoded = cli._json_value(asdict(population))
    serialized = json.dumps(encoded, sort_keys=True, allow_nan=False)
    assert '"amount": "100.00"' in serialized
    assert '"net_amount": "98.00"' in serialized
    assert '"source": "verified_webhook"' in serialized
    assert str(PAYMENT) in serialized
    assert AT.isoformat() in serialized
    assert '"qualified_payment_ids": []' in serialized


def test_capture_reads_whole_populations_without_payloads_or_inner_join_loss() -> None:
    db = Mock(spec=Session)
    orphan = replace(event(), payment_id=None)
    inbox = (
        UUID(int=10),
        UUID(int=11),
        UUID(int=12),
        "receipt-event",
        "charge.success",
        "e" * 64,
        "processed",
        str(event().id),
        str(PAYMENT),
        str(PROVIDER),
        None,
        AT,
        AT,
        AT,
        AT,
    )
    db.execute.side_effect = [
        (tuple(asdict(payment()).values()),),
        (),
        (tuple(asdict(event()).values()), tuple(asdict(orphan).values())),
        (),
        (),
        (),
        (),
        (),
        (inbox,),
    ]
    result = owner._cash_evidence(db, query=QUERY, report=report())
    assert len(result.provider_events) == 2
    assert result.provider_events[1].payment_id is None
    assert result.inbox_receipts[0].consequence_provider_event_id == event().id
    counts = {row.table.value: row.count for row in result.source_counts}
    assert counts["payments"] == 1
    assert counts["payment_provider_events"] == 2
    assert counts["integration_inbox"] == 1
    assert sum(counts.values()) == 4
    for call in db.execute.call_args_list:
        statement = call.args[0]
        sql = str(statement)
        assert " WHERE " not in sql and " JOIN " not in sql
        assert "ORDER BY" in sql
        selected = {column.name for column in statement.selected_columns}
        assert not selected & {
            "payload",
            "payload_json",
            "headers_json",
            "error",
            "error_detail",
            "file_path",
            "review_notes",
        }
    assert result.qualified_payment_ids == ()
    db.flush.assert_not_called()
    db.commit.assert_not_called()


def test_refund_missing_event_and_ledger_are_retained_as_provenance_blockers() -> None:
    from app.models.billing import PaymentRefundOrigin
    from app.services.billing.opening_export_contracts import CashRefundEvidence

    movement = CashRefundEvidence(
        UUID(int=20),
        PAYMENT,
        UUID(int=21),
        UUID(int=22),
        None,
        UUID(int=23),
        Decimal("10.00"),
        "NGN",
        PaymentRefundOrigin.provider_event,
        "f" * 64,
        AT,
    )
    population = replace(cash(), refunds=(movement,))
    result = owner._classify_cash(population, query=QUERY, report=report())
    assert result.refunds == (movement,)
    codes = {e.code for e in result.blocking_exceptions if e.source_id == movement.id}
    assert {
        ExceptionCode.settlement_mapping,
        ExceptionCode.settlement_provenance,
        ExceptionCode.cash_unqualified,
    } <= codes


def test_cli_v2_cash_blockers_fail_readiness_even_with_empty_ar_population(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    from app.services.billing.opening_export_contracts import (
        BillingOpeningExportPreviewV2,
    )

    db = Mock(spec=Session)

    @contextmanager
    def snapshot() -> Iterator[Session]:
        yield db

    base = report()
    population = owner._classify_cash(cash(), query=QUERY, report=base)
    extended = BillingOpeningExportPreviewV2(
        base.capture,
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
        "f" * 64,
        population,
    )
    query_owner = Mock(return_value=extended)
    monkeypatch.setattr(cli, "read_only_snapshot_session", snapshot)
    monkeypatch.setattr(cli, "preview_billing_opening_export", query_owner)
    assert (
        cli.main(
            [
                "--source-repository-sha",
                QUERY.source_repository_sha,
                "--installation-id",
                str(QUERY.installation_id),
                "--economic-cutoff",
                AT.isoformat(),
                "--currency-precision",
                "NGN:2",
                "--population-version",
                QUERY.population_version,
            ]
        )
        == 1
    )
    query_owner.assert_called_once_with(db, query=QUERY)
    output = json.loads(capsys.readouterr().out)
    assert output["cash_evidence"]["qualified_payment_ids"] == []
    assert (
        output["capture"]["declared_source"]["population_version"]
        == QUERY.population_version
    )
