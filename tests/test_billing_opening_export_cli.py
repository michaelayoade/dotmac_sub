"""Operator boundary canaries; hosted PostgreSQL owns snapshot acceptance."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, Mock
from uuid import UUID

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import db as app_db
from app.services.billing.opening_export_contracts import (
    BillingOpeningExportPreview,
    CaptureEvidence,
    ExceptionCode,
    OpeningControl,
    OpeningException,
    PreviewBillingOpeningExportQuery,
)
from scripts.migration import billing_opening_export as cli

AT = datetime(2026, 10, 13, tzinfo=UTC)
QUERY = PreviewBillingOpeningExportQuery("a" * 40, UUID(int=123), AT, (("NGN", 2),))
ARGS = [
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


@pytest.fixture()
def controlled_session(monkeypatch: pytest.MonkeyPatch) -> Mock:
    db = MagicMock(spec=Session)
    db.get_bind.return_value.dialect.name = "postgresql"
    db.in_transaction.return_value = True
    db.new = set()
    db.dirty = set()
    db.deleted = set()
    monkeypatch.setattr(app_db, "SessionLocal", Mock(return_value=db))
    return db


def preview() -> BillingOpeningExportPreview:
    return BillingOpeningExportPreview(
        capture=CaptureEvidence(
            QUERY, ("head",), "snapshot", AT, "repeatable read", True
        ),
        subscriber_ids=(UUID(int=1),),
        invoices=(),
        lines=(),
        settlements=(),
        financial_sources=(),
        lanes=(),
        effects=(),
        eligible_invoice_ids=(),
        exclusions=(),
        blocking_exceptions=(),
        controls=(
            OpeningControl(
                UUID(int=1),
                "NGN",
                0,
                0,
                0,
                0,
                Decimal("0.10"),
                Decimal("0"),
                Decimal("0.10"),
                Decimal("0"),
                0,
            ),
        ),
        content_fingerprint="f" * 64,
    )


def assert_released(db: Mock) -> None:
    db.rollback.assert_called_once_with()
    db.close.assert_called_once_with()
    for name in ("add", "add_all", "delete", "flush", "commit", "begin_nested"):
        getattr(db, name).assert_not_called()


@pytest.mark.parametrize("blocked", [False, True])
def test_cli_delegates_typed_query_in_canonical_snapshot_and_encodes_exact_evidence(
    controlled_session: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    blocked: bool,
) -> None:
    report = preview()
    if blocked:
        report = replace(
            report,
            blocking_exceptions=(
                OpeningException(
                    ExceptionCode.unbacked_lane,
                    UUID(int=1),
                    "NGN",
                    UUID(int=2),
                    "Unbacked",
                ),
            ),
        )

    def census(
        db: Session, *, query: PreviewBillingOpeningExportQuery
    ) -> BillingOpeningExportPreview:
        assert db is controlled_session
        assert query == QUERY
        assert not db.new and not db.dirty and not db.deleted
        controlled_session.connection.assert_called_once_with(
            execution_options=dict(app_db.READ_ONLY_SNAPSHOT_OPTIONS)
        )
        assert app_db.READ_ONLY_SNAPSHOT_OPTIONS["isolation_level"] == "REPEATABLE READ"
        assert app_db.READ_ONLY_SNAPSHOT_OPTIONS["postgresql_readonly"] is True
        return report

    owner = Mock(side_effect=census)
    monkeypatch.setattr(cli, "preview_billing_opening_export", owner)
    assert cli.main(ARGS) == int(blocked)
    owner.assert_called_once_with(controlled_session, query=QUERY)
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert output == json.dumps(payload, sort_keys=True, allow_nan=False) + "\n"
    assert payload["controls"][0]["original_total"] == "0.10"
    assert payload["capture"]["captured_at"] == AT.isoformat()
    assert payload["subscriber_ids"] == [str(UUID(int=1))]
    assert payload["capture"]["declared_source"]["installation_id"] == str(
        QUERY.installation_id
    )
    if blocked:
        assert payload["blocking_exceptions"][0]["code"] == "unbacked_lane"
    assert_released(controlled_session)


def test_cli_real_owner_refuses_incorrect_snapshot_and_releases_session(
    controlled_session: Mock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    controlled_session.execute.return_value.one.return_value = (
        "read committed",
        "off",
        "snapshot",
        AT,
    )
    assert cli.main(ARGS) == 2
    assert json.loads(capsys.readouterr().out) == {
        "complete": False,
        "error": "financial.invoices.opening_export.snapshot_required",
    }
    assert_released(controlled_session)


def test_cli_database_failure_is_sanitized_and_releases_session(
    controlled_session: Mock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    controlled_session.connection.side_effect = SQLAlchemyError(
        "private database details"
    )
    assert cli.main(ARGS) == 2
    assert json.loads(capsys.readouterr().out) == {
        "complete": False,
        "error": "snapshot_unavailable",
    }
    assert_released(controlled_session)


@pytest.mark.parametrize(
    "option,value",
    [
        ("--source-repository-sha", "main"),
        ("--installation-id", "invalid"),
        ("--economic-cutoff", "2026-10-13T00:00:00"),
        ("--currency-precision", "NGN:3"),
        ("--population-version", "future"),
    ],
)
def test_invalid_coordinates_never_open_session(
    controlled_session: Mock,
    option: str,
    value: str,
) -> None:
    args = list(ARGS)
    args[args.index(option) + 1] = value
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2
    controlled_session.connection.assert_not_called()
    controlled_session.close.assert_not_called()


def test_duplicate_precision_never_opens_session(controlled_session: Mock) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main([*ARGS, "--currency-precision", "NGN:2"])
    assert error.value.code == 2
    controlled_session.connection.assert_not_called()


@pytest.mark.parametrize(
    "value", [1.25, Decimal("NaN"), datetime(2026, 10, 13), object()]
)
def test_unsupported_evidence_is_not_silently_stringified(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        cli._json_value(value)


@pytest.mark.parametrize("pending", ["new", "dirty", "deleted"])
def test_cli_real_owner_refuses_pending_orm_state(
    controlled_session: Mock,
    capsys: pytest.CaptureFixture[str],
    pending: str,
) -> None:
    setattr(controlled_session, pending, {object()})
    assert cli.main(ARGS) == 2
    assert json.loads(capsys.readouterr().out)["error"].endswith("snapshot_required")
    controlled_session.execute.assert_not_called()
    assert_released(controlled_session)


def test_serialization_failure_does_not_emit_partial_evidence(
    controlled_session: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = preview()
    report = replace(
        report, capture=replace(report.capture, captured_at=datetime(2026, 10, 13))
    )
    monkeypatch.setattr(
        cli, "preview_billing_opening_export", Mock(return_value=report)
    )
    assert cli.main(ARGS) == 2
    assert json.loads(capsys.readouterr().out) == {
        "complete": False,
        "error": "evidence_serialization_failed",
    }
    assert_released(controlled_session)
