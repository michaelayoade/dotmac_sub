"""D1 hold: one kill switch routes every Starter ledger path back to legacy.

``billing.starter_ledger_live_enabled`` defaults OFF and is owner-command-only.
OFF: issuance eligibility short-circuits to the legacy lane and the Starter
settlement lane returns before reading or writing any Starter table.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.billing import InvoiceStatus, PaymentStatus
from app.models.domain_settings import SettingDomain
from app.services import payment_webhook_commands as webhooks
from app.services import settings_spec
from app.services import web_billing_invoices as web_invoices
from app.services.billing import starter_issuance as issuance

_KEY = "starter_ledger_live_enabled"


class _ExplodingDb:
    """Any use means a Starter table (or anything else) was touched."""

    def __getattr__(self, name: str):  # noqa: ANN204
        raise AssertionError(f"database touched while switch OFF: {name}")


def test_spec_is_default_off_and_owner_command_only() -> None:
    spec = settings_spec.get_spec(SettingDomain.billing, _KEY)
    assert spec is not None
    assert spec.default is False
    assert spec.owner_command_only is True
    assert spec.inherits is False


@pytest.mark.parametrize("raw", [None, False, "true", 1, "", "on"])
def test_reader_fails_closed_for_anything_but_boolean_true(monkeypatch, raw) -> None:
    monkeypatch.setattr(settings_spec, "resolve_value", lambda *_a, **_k: raw)
    assert settings_spec.starter_ledger_live_enabled(object()) is False


def test_reader_is_on_only_for_boolean_true(monkeypatch) -> None:
    monkeypatch.setattr(settings_spec, "resolve_value", lambda *_a, **_k: True)
    assert settings_spec.starter_ledger_live_enabled(object()) is True


def test_eligibility_short_circuits_when_off(monkeypatch) -> None:
    monkeypatch.setattr(issuance, "starter_ledger_live_enabled", lambda _db: False)
    reason = issuance.eligibility_reason(_ExplodingDb(), object())  # type: ignore[arg-type]
    assert reason is not None and "OFF" in reason


def _issue_with_switch(monkeypatch, *, live: bool) -> list[str]:
    """Run the detail Issue action; return which writer lane was entered."""

    invoice = SimpleNamespace(id=uuid4(), status=InvoiceStatus.draft)
    calls: list[str] = []
    real_reason = issuance.eligibility_reason
    monkeypatch.setattr(issuance, "starter_ledger_live_enabled", lambda _db: live)
    monkeypatch.setattr(
        web_invoices.billing_service.invoices, "get", lambda **_k: invoice
    )
    monkeypatch.setattr(
        web_invoices.web_prepaid_draft_reconciliation_service,
        "preview_for_invoice_detail",
        lambda *_a, **_k: None,
    )
    # ON: stand in for the separately tested eligibility rules so this test
    # isolates the routing decision. OFF: the real short-circuit runs.
    monkeypatch.setattr(
        issuance,
        "eligibility_reason",
        (lambda db, inv: None) if live else real_reason,
    )
    monkeypatch.setattr(
        web_invoices.starter_issuance_service,
        "issue_admin_invoice_via_starter",
        lambda *_a, **_k: calls.append("starter"),
    )

    def _legacy(*_a, **_k):  # noqa: ANN202
        calls.append("legacy")
        raise RuntimeError("legacy-sentinel")

    monkeypatch.setattr(
        web_invoices.billing_service.invoices, "issue_draft_system", _legacy
    )
    db = SimpleNamespace(commit=lambda: None, refresh=lambda _o: None)
    with pytest.raises(RuntimeError) if not live else _nullcontext():
        web_invoices.issue_invoice_from_detail(db, invoice_id=invoice.id)  # type: ignore[arg-type]
    return calls


class _nullcontext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> bool:
        return False


def test_detail_issue_goes_legacy_when_off(monkeypatch) -> None:
    assert _issue_with_switch(monkeypatch, live=False) == ["legacy"]


def test_detail_issue_goes_through_starter_when_on(monkeypatch) -> None:
    assert _issue_with_switch(monkeypatch, live=True) == ["starter"]


def _prepared() -> SimpleNamespace:
    return SimpleNamespace(
        settlement=SimpleNamespace(status=PaymentStatus.succeeded, amount=1),
        topup_intent=SimpleNamespace(invoice_id=uuid4(), purpose="invoice_payment"),
        ingest=SimpleNamespace(external_id="txn"),
    )


def test_settlement_lane_is_inert_when_off(monkeypatch) -> None:
    monkeypatch.setattr(webhooks, "starter_ledger_live_enabled", lambda _db: False)
    result = webhooks._stage_starter_invoice_settlement(
        _ExplodingDb(),  # type: ignore[arg-type]
        prepared=_prepared(),  # type: ignore[arg-type]
    )
    assert result is None


def test_settlement_lane_reads_link_when_on(monkeypatch) -> None:
    monkeypatch.setattr(webhooks, "starter_ledger_live_enabled", lambda _db: True)
    queried: list[object] = []
    db = SimpleNamespace(scalar=lambda stmt: queried.append(stmt))
    result = webhooks._stage_starter_invoice_settlement(
        db,  # type: ignore[arg-type]
        prepared=_prepared(),  # type: ignore[arg-type]
    )
    assert result is None  # no link row -> legacy lane, but the lookup ran
    assert len(queried) == 1
