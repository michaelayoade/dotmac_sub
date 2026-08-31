from __future__ import annotations

import inspect
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.payment_reconciliation import (
    PAYSTACK_OUTSIDE_WINDOW_RECOVERY_SCOPE,
    ConfirmPaystackOutsideWindowRecoveryCommand,
    PreviewPaystackOutsideWindowRecoveryQuery,
)
from scripts.billing import reconcile_paystack_reference as command


class _Value(str, Enum):
    recoverable = "recoverable"
    recovered = "recovered"
    pending = "pending"
    success = "success"
    provider_reported_success = "provider_reported_success"


def _preview(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "intent_id": uuid4(),
        "reference": "ps_ref_reviewed",
        "disposition": _Value.recoverable,
        "actionable": True,
        "fingerprint": "a" * 64,
        "intent_status": _Value.pending,
        "intent_created_at": datetime(2026, 8, 1, 10, 30, tzinfo=UTC),
        "provider_id": uuid4(),
        "checkout_binding_id": uuid4(),
        "provider_external_id": "provider-transaction-42",
        "gross_amount": Decimal("1050.00"),
        "provider_fee": Decimal("50.00"),
        "authorized_net_amount": Decimal("1000.00"),
        "currency": "NGN",
        "provider_status": _Value.success,
        "reason_code": _Value.provider_reported_success,
        "existing_payment_id": None,
        "raw": {"authorization": {"token": "must-not-leak"}},
        "metadata": {"customer": "must-not-leak"},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _result(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "recovery_run_id": uuid4(),
        "intent_id": uuid4(),
        "disposition": _Value.recovered,
        "payment_id": uuid4(),
        "preview_fingerprint": "b" * 64,
        "replayed": False,
        "raw": {"authorization": "must-not-leak"},
        "metadata": {"customer": "must-not-leak"},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_preview_uses_read_session_and_emits_only_typed_preview_fields(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    intent_id = uuid4()
    preview = _preview(intent_id=intent_id)
    read_db = object()
    observed: list[str] = []
    captured_query: list[PreviewPaystackOutsideWindowRecoveryQuery] = []

    @contextmanager
    def read_session():
        observed.append("read")
        yield read_db

    @contextmanager
    def forbidden_owner_session():
        observed.append("owner")
        raise AssertionError("preview opened a write-capable session")
        yield  # pragma: no cover

    def preview_recovery(
        db: object,
        query: PreviewPaystackOutsideWindowRecoveryQuery,
    ) -> SimpleNamespace:
        assert db is read_db
        captured_query.append(query)
        return preview

    monkeypatch.setattr(command.db_session_adapter, "read_session", read_session)
    monkeypatch.setattr(
        command.db_session_adapter,
        "owner_command_session",
        forbidden_owner_session,
    )
    monkeypatch.setattr(
        command,
        "preview_paystack_outside_window_recovery",
        preview_recovery,
    )

    exit_code = command.main(
        ["--intent-id", str(intent_id), "--reference", preview.reference]
    )

    assert exit_code == 0
    assert observed == ["read"]
    query = captured_query[0]
    assert query.intent_id == intent_id
    assert query.reference == preview.reference
    assert query.observed_at.tzinfo is UTC
    assert query.observed_at <= datetime.now(UTC)
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "actionable": True,
        "authorized_net_amount": "1000.00",
        "checkout_binding_id": str(preview.checkout_binding_id),
        "currency": "NGN",
        "disposition": "recoverable",
        "existing_payment_id": None,
        "fingerprint": "a" * 64,
        "gross_amount": "1050.00",
        "intent_created_at": "2026-08-01T10:30:00+00:00",
        "intent_id": str(intent_id),
        "intent_status": "pending",
        "provider_external_id": "provider-transaction-42",
        "provider_fee": "50.00",
        "provider_id": str(preview.provider_id),
        "provider_status": "success",
        "reason_code": "provider_reported_success",
        "reference": preview.reference,
    }
    assert "raw" not in payload
    assert "metadata" not in payload


@pytest.mark.parametrize(
    "argv",
    [[], ["--intent-id", str(uuid4())], ["--reference", "ps_ref_reviewed"]],
)
def test_preview_requires_exact_intent_and_reference(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        command.main(argv)

    assert exc_info.value.code == 2


def test_apply_uses_owner_session_and_emits_only_typed_result_fields(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    intent_id = uuid4()
    payment_id = uuid4()
    result = _result(intent_id=intent_id, payment_id=payment_id)
    owner_db = object()
    observed: list[str] = []
    captured_commands: list[ConfirmPaystackOutsideWindowRecoveryCommand] = []

    @contextmanager
    def forbidden_read_session():
        observed.append("read")
        raise AssertionError("apply opened a read-only session")
        yield  # pragma: no cover

    @contextmanager
    def owner_session():
        observed.append("owner")
        yield owner_db

    def confirm_recovery(
        db: object,
        recovery_command: ConfirmPaystackOutsideWindowRecoveryCommand,
    ) -> SimpleNamespace:
        assert db is owner_db
        captured_commands.append(recovery_command)
        return result

    monkeypatch.setattr(
        command.db_session_adapter,
        "read_session",
        forbidden_read_session,
    )
    monkeypatch.setattr(
        command.db_session_adapter,
        "owner_command_session",
        owner_session,
    )
    monkeypatch.setattr(
        command,
        "confirm_paystack_outside_window_recovery",
        confirm_recovery,
    )

    exit_code = command.main(
        [
            "--intent-id",
            str(intent_id),
            "--reference",
            "ps_ref_reviewed",
            "--apply",
            "--fingerprint",
            result.preview_fingerprint,
            "--actor",
            "finance.operator@example.com",
            "--reason",
            "Approved after settlement evidence review",
            "--review-reference",
            "FIN-2026-0819",
            "--idempotency-key",
            "paystack-recovery:FIN-2026-0819",
            "--confirm",
            "APPLY_REVIEWED_PAYSTACK_RECOVERY",
        ]
    )

    assert exit_code == 0
    assert observed == ["owner"]
    recovery_command = captured_commands[0]
    assert recovery_command.intent_id == intent_id
    assert recovery_command.reference == "ps_ref_reviewed"
    assert recovery_command.preview_fingerprint == result.preview_fingerprint
    assert recovery_command.review_reference == "FIN-2026-0819"
    assert recovery_command.confirmed is True
    assert recovery_command.context.actor == "finance.operator@example.com"
    assert recovery_command.context.reason == (
        "Approved after settlement evidence review"
    )
    assert recovery_command.context.scope == PAYSTACK_OUTSIDE_WINDOW_RECOVERY_SCOPE
    assert recovery_command.context.idempotency_key == (
        "paystack-recovery:FIN-2026-0819"
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "disposition": "recovered",
        "intent_id": str(intent_id),
        "payment_id": str(payment_id),
        "preview_fingerprint": "b" * 64,
        "recovery_run_id": str(result.recovery_run_id),
        "replayed": False,
    }
    assert "raw" not in payload
    assert "metadata" not in payload


@pytest.mark.parametrize(
    "confirmation",
    [None, "apply_reviewed_paystack_recovery", "APPLY_REVIEWED_PAYSTACK_RECOVER"],
)
def test_apply_rejects_missing_or_inexact_confirmation_before_opening_session(
    monkeypatch: pytest.MonkeyPatch,
    confirmation: str | None,
) -> None:
    opened = False

    @contextmanager
    def owner_session():
        nonlocal opened
        opened = True
        yield object()

    monkeypatch.setattr(
        command.db_session_adapter,
        "owner_command_session",
        owner_session,
    )
    argv = [
        "--intent-id",
        str(uuid4()),
        "--reference",
        "ps_ref_reviewed",
        "--apply",
        "--fingerprint",
        "c" * 64,
        "--actor",
        "finance.operator@example.com",
        "--reason",
        "Reviewed",
        "--review-reference",
        "FIN-2026-0819",
        "--idempotency-key",
        "paystack-recovery:FIN-2026-0819",
    ]
    if confirmation is not None:
        argv.extend(["--confirm", confirmation])

    with pytest.raises(SystemExit) as exc_info:
        command.main(argv)

    assert exc_info.value.code == 2
    assert opened is False


@pytest.mark.parametrize(
    "omitted_option",
    [
        "--fingerprint",
        "--actor",
        "--reason",
        "--review-reference",
        "--idempotency-key",
    ],
)
def test_apply_requires_all_review_evidence(
    omitted_option: str,
) -> None:
    option_values = {
        "--fingerprint": "d" * 64,
        "--actor": "finance.operator@example.com",
        "--reason": "Reviewed",
        "--review-reference": "FIN-2026-0819",
        "--idempotency-key": "paystack-recovery:FIN-2026-0819",
        "--confirm": "APPLY_REVIEWED_PAYSTACK_RECOVERY",
    }
    argv = [
        "--intent-id",
        str(uuid4()),
        "--reference",
        "ps_ref_reviewed",
        "--apply",
    ]
    for option, value in option_values.items():
        if option != omitted_option:
            argv.extend([option, value])

    with pytest.raises(SystemExit) as exc_info:
        command.main(argv)

    assert exc_info.value.code == 2


def test_cli_contains_no_financial_writes_or_orm_access() -> None:
    source = inspect.getsource(command)

    for forbidden in (
        "app.models",
        "sqlalchemy",
        "SessionLocal",
        "sessionmaker",
        ".commit(",
        ".rollback(",
        ".add(",
        ".execute(",
        ".query(",
    ):
        assert forbidden not in source
