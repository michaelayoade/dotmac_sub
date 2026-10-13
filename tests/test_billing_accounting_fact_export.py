"""Released Billing accounting facts cross the transport boundary losslessly."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from dotmac_billing import (
    AccountingAllocationEffectV1,
    AccountingEffectV1,
    AccountingFactV1,
    AppliedFxSnapshotV1,
    AppliedTaxSnapshotV1,
)
from dotmac_kernel.cache import PlatformScope, TenantScope
from dotmac_kernel.money import Currency, Money

from app.services.dotmac_erp.billing_accounting_fact import (
    AllocationKind,
    BillingAccountingExportV1,
    BillingAccountingFactError,
    EffectKind,
    map_billing_accounting_fact,
    parse_billing_accounting_fact,
)

INSTANT = datetime(2026, 10, 13, 12, tzinfo=UTC)


def accounting_fact() -> AccountingFactV1:
    money = Money.of("120.50", Currency("NGN", 2))
    return AccountingFactV1(
        scope=TenantScope(uuid4()),
        source_system="billing",
        fact_id=uuid4(),
        fact_version=1,
        billing_account_id=uuid4(),
        posting_group_id=uuid4(),
        source_ref="opaque:never:parse",
        source_authority="internal",
        effect_kind="reversal",
        occurred_at=INSTANT,
        committed_at=INSTANT,
        effects=(AccountingEffectV1("receivable", money),),
        allocations=(
            AccountingAllocationEffectV1(
                settlement_id=uuid4(),
                document_id=uuid4(),
                effect_kind="reversal",
                amount_delta=money,
                offsets_allocation_id=uuid4(),
            ),
        ),
        reverses_fact_id=uuid4(),
        tax_snapshots=(
            AppliedTaxSnapshotV1(
                treatment_code="VAT",
                jurisdiction_code="NG",
                policy_id="tax-policy",
                policy_version="v3",
                rate=Decimal("0.075"),
                taxable_basis=money,
                tax_amount=Money.of("9.04", Currency("NGN", 2)),
            ),
        ),
        fx_snapshot=AppliedFxSnapshotV1(
            observation_id="fx-123",
            observation_version="v4",
            base_currency="USD",
            quote_currency="NGN",
            rate=Decimal("1650.123456789"),
            rate_purpose="accounting",
            observed_at=INSTANT,
            effective_at=INSTANT,
            rounding_policy="half-up",
            provenance="finance-reviewed",
        ),
    )


@pytest.mark.parametrize(
    "kind",
    (
        "invoice_issued",
        "credit_note_issued",
        "settlement_accepted",
        "allocation",
        "deallocation",
        "reallocation",
        "refund",
        "reversal",
    ),
)
def test_every_released_effect_round_trips_without_accounting_inference(
    kind: EffectKind,
) -> None:
    fact = replace(accounting_fact(), effect_kind=kind)
    envelope = map_billing_accounting_fact(fact)
    payload = envelope.to_payload()
    assert payload["contract"] == "billing.accounting.export.v1"
    assert payload["fact_contract"] == "billing.accounting.fact.v1"
    assert parse_billing_accounting_fact(json.dumps(payload["fact"])) == fact
    assert envelope.fact == fact
    assert "lines" not in payload
    assert "gl_account" not in json.dumps(payload)


@pytest.mark.parametrize("field", ("amount", "tax", "reversal", "scope", "version"))
def test_complete_fact_digest_detects_changes(field: str) -> None:
    fact = accounting_fact()
    changed = {
        "amount": replace(
            fact,
            effects=(
                AccountingEffectV1("receivable", Money.of("121", Currency("NGN"))),
            ),
        ),
        "tax": replace(fact, tax_snapshots=()),
        "reversal": replace(fact, reverses_fact_id=uuid4()),
        "scope": replace(fact, scope=PlatformScope()),
        "version": replace(fact, fact_version=2),
    }[field]
    assert (
        map_billing_accounting_fact(fact).fact_digest
        != map_billing_accounting_fact(changed).fact_digest
    )


@pytest.mark.parametrize(
    "changes",
    (
        {"fact_version": 0},
        {"fact_version": True},
        {"occurred_at": INSTANT.replace(tzinfo=None)},
        {"committed_at": INSTANT.replace(tzinfo=None)},
        {"source_system": ""},
    ),
)
def test_invalid_fact_boundaries_fail_closed(changes: dict[str, object]) -> None:
    with pytest.raises(BillingAccountingFactError):
        map_billing_accounting_fact(replace(accounting_fact(), **changes))


def test_canonical_digest_ignores_json_key_order() -> None:
    envelope = map_billing_accounting_fact(accounting_fact())
    parsed = parse_billing_accounting_fact(
        json.dumps(envelope.to_payload()["fact"], sort_keys=True)
    )
    assert map_billing_accounting_fact(parsed).fact_digest == envelope.fact_digest


def test_preview_cli_calls_real_mapper_without_opening_a_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.services.dotmac_erp import billing_accounting_fact
    from scripts.billing import billing_target_shadow

    fact = accounting_fact()
    captured = tmp_path / "fact.json"
    captured.write_text(
        json.dumps(map_billing_accounting_fact(fact).to_payload()["fact"])
    )
    calls: list[AccountingFactV1] = []
    actual_mapper = billing_accounting_fact.map_billing_accounting_fact

    def observed_mapper(value: AccountingFactV1) -> BillingAccountingExportV1:
        calls.append(value)
        return actual_mapper(value)

    def forbidden_session() -> None:
        raise AssertionError("Pure preview must not open a database session")

    monkeypatch.setattr(
        billing_accounting_fact, "map_billing_accounting_fact", observed_mapper
    )
    monkeypatch.setattr(billing_target_shadow, "SessionLocal", forbidden_session)
    monkeypatch.setattr(
        "sys.argv",
        [
            "billing-target-shadow",
            "preview-module-accounting-export",
            "--fact-file",
            str(captured),
        ],
    )
    assert billing_target_shadow.main() == 0
    assert calls == [fact]
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "captured-preview-only"
    assert output["runtime_consumer"] == "pending"
    assert output["export"]["fact_digest"] == actual_mapper(fact).fact_digest


@pytest.mark.parametrize(
    "kind", ("allocation", "deallocation", "reallocation", "refund", "reversal")
)
def test_allocation_effect_kinds_preserve_offsets_and_document_identity(
    kind: AllocationKind,
) -> None:
    fact = accounting_fact()
    fact = replace(fact, allocations=(replace(fact.allocations[0], effect_kind=kind),))
    payload = map_billing_accounting_fact(fact).to_payload()
    assert parse_billing_accounting_fact(json.dumps(payload["fact"])) == fact


@pytest.mark.parametrize(
    "bad_money",
    (
        {"amount": 12.5, "currency": "NGN", "minor_units": 2},
        {"amount": "12.501", "currency": "NGN", "minor_units": 2},
        {"amount": "NaN", "currency": "NGN", "minor_units": 2},
        {"amount": "12.50", "currency": "ngn", "minor_units": 2},
        {"amount": "12.50", "currency": "NGN", "minor_units": True},
    ),
)
def test_parser_refuses_inexact_money_and_ambiguous_currency(
    bad_money: dict[str, str | int | float | bool],
) -> None:
    payload = map_billing_accounting_fact(accounting_fact()).to_payload()["fact"]
    payload["effects"][0]["amount_delta"] = bad_money
    with pytest.raises(BillingAccountingFactError):
        parse_billing_accounting_fact(json.dumps(payload))


def test_scope_source_and_version_define_distinct_fact_replay_identities() -> None:
    fact = accounting_fact()
    original = map_billing_accounting_fact(fact)
    for changed in (
        replace(fact, scope=PlatformScope()),
        replace(fact, scope=TenantScope(uuid4())),
        replace(fact, source_system="another-source"),
        replace(fact, fact_version=2),
    ):
        envelope = map_billing_accounting_fact(changed)
        assert envelope.idempotency_key != original.idempotency_key
        assert (
            parse_billing_accounting_fact(json.dumps(envelope.to_payload()["fact"]))
            == changed
        )


@pytest.mark.parametrize("snapshot", ("tax", "fx"))
def test_rates_beyond_decimal_context_precision_round_trip_and_do_not_collide(
    snapshot: str,
) -> None:
    fact = accounting_fact()
    first_rate = Decimal("0.1234567890123456789012345678901234567891")
    second_rate = Decimal("0.1234567890123456789012345678901234567892")
    if snapshot == "tax":
        first = replace(
            fact, tax_snapshots=(replace(fact.tax_snapshots[0], rate=first_rate),)
        )
        second = replace(
            fact, tax_snapshots=(replace(fact.tax_snapshots[0], rate=second_rate),)
        )
    else:
        assert fact.fx_snapshot is not None
        first = replace(fact, fx_snapshot=replace(fact.fx_snapshot, rate=first_rate))
        second = replace(fact, fx_snapshot=replace(fact.fx_snapshot, rate=second_rate))
    first_export = map_billing_accounting_fact(first)
    second_export = map_billing_accounting_fact(second)
    assert (
        parse_billing_accounting_fact(json.dumps(first_export.to_payload()["fact"]))
        == first
    )
    assert (
        parse_billing_accounting_fact(json.dumps(second_export.to_payload()["fact"]))
        == second
    )
    assert first_export.fact_digest != second_export.fact_digest
