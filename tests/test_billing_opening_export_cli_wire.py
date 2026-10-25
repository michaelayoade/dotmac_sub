"""``--output wire`` renders exactly the #3706 serializer; default stays JSON."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock
from uuid import UUID

import pytest

from app.services.billing.opening_census_wire import render_opening_census_wire
from scripts.migration import billing_opening_export as cli
from tests.test_billing_opening_export_cli import (  # noqa: F401
    ARGS,
    controlled_session,
)
from tests.test_opening_census_wire import _preview

V3 = "sub-billing-opening-census-v3"
WAT = timezone(timedelta(hours=1))


def _args(*extra: str) -> list[str]:
    args = list(ARGS)
    args[args.index("--population-version") + 1] = V3
    return [*args, *extra]


def _patch(monkeypatch: pytest.MonkeyPatch, report: object) -> Mock:
    owner = Mock(return_value=report)
    monkeypatch.setattr(cli, "preview_billing_opening_export", owner)
    return owner


def _non_utc_exponent_preview():
    preview = _preview()
    invoice = replace(
        preview.invoices[0],
        # str() renders "1.5E+2"; format(..., "f") renders "150".
        total=Decimal("1.5E+2"),
        issued_at=datetime(2026, 10, 18, 1, 0, tzinfo=WAT),
    )
    return replace(preview, invoices=(invoice,))


def test_wire_output_is_byte_identical_to_the_wire_serializer(
    controlled_session: Mock,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _non_utc_exponent_preview()
    _patch(monkeypatch, report)
    code = cli.main(_args("--output", "wire"))
    out = capsys.readouterr().out
    assert out == render_opening_census_wire(report)
    assert out.endswith("}\n") and not out.endswith("\n\n")
    # exit code semantics are unchanged: cash blockers exist in the fixture
    assert code == 1


def test_wire_uses_format_f_and_utc_where_default_json_does_not(
    controlled_session: Mock,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _non_utc_exponent_preview()
    _patch(monkeypatch, report)

    cli.main(_args("--output", "wire"))
    wire = json.loads(capsys.readouterr().out)["invoices"][0]
    cli.main(_args())
    legacy = json.loads(capsys.readouterr().out)["invoices"][0]

    assert wire["total"] == format(Decimal("1.5E+2"), "f") == "150"
    assert legacy["total"] == str(Decimal("1.5E+2")) == "1.5E+2"
    assert wire["issued_at"] == "2026-10-18T00:00:00+00:00"
    assert legacy["issued_at"] == "2026-10-18T01:00:00+01:00"


def test_default_output_is_unchanged_json(
    controlled_session: Mock,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch(monkeypatch, _preview())
    cli.main(_args())
    default = capsys.readouterr().out
    cli.main(_args("--output", "json"))
    explicit = capsys.readouterr().out
    assert default == explicit
    payload = json.loads(default)
    assert default == json.dumps(payload, sort_keys=True, allow_nan=False) + "\n"
    assert ", " in default or ": " in default  # default separators, not compact


def test_wire_round_trips_through_strict_adapter_parsing_rules(
    controlled_session: Mock,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mirror of Starter's adapter input rules (money strings, aware instants,
    UUID text, no duplicate keys); the real adapter run is checked in the PR."""
    report = _non_utc_exponent_preview()
    _patch(monkeypatch, report)
    cli.main(_args("--output", "wire"))
    text = capsys.readouterr().out

    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        keys = [key for key, _ in pairs]
        assert len(keys) == len(set(keys))
        return dict(pairs)

    payload = json.loads(
        text,
        object_pairs_hook=no_duplicates,
        parse_constant=lambda _c: pytest.fail("non-finite constant"),
    )
    invoice = payload["invoices"][0]
    assert type(invoice["total"]) is str
    assert Decimal(invoice["total"]) == report.invoices[0].total
    assert "E" not in invoice["total"].upper()
    issued = datetime.fromisoformat(invoice["issued_at"])
    assert issued.utcoffset() == timedelta(0)
    assert issued == report.invoices[0].issued_at
    assert UUID(invoice["invoice_id"]) == report.invoices[0].invoice_id
    declared = payload["capture"]["declared_source"]
    assert declared["population_version"] == V3
    assert declared["currency_precisions"] == [["NGN", 2]]
    # Derived values on the wire are exactly the report's, including the
    # fingerprint that makes two runs comparable.
    assert payload["content_fingerprint"] == report.content_fingerprint
    assert datetime.fromisoformat(payload["capture"]["captured_at"]).tzinfo == UTC


@pytest.mark.parametrize(
    "version", ["sub-billing-opening-census-v1", "sub-billing-opening-census-v2"]
)
def test_wire_requires_census_v3_before_any_session_opens(
    controlled_session: Mock,  # noqa: F811
    version: str,
) -> None:
    args = list(ARGS)
    args[args.index("--population-version") + 1] = version
    with pytest.raises(SystemExit) as error:
        cli.main([*args, "--output", "wire"])
    assert error.value.code == 2
    controlled_session.connection.assert_not_called()


def test_wire_failure_emits_no_partial_document(
    controlled_session: Mock,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    preview = _preview()
    naive = replace(preview.invoices[0], issued_at=datetime(2026, 10, 18))
    _patch(monkeypatch, replace(preview, invoices=(naive,)))
    assert cli.main(_args("--output", "wire")) == 2
    assert json.loads(capsys.readouterr().out) == {
        "complete": False,
        "error": "evidence_serialization_failed",
    }


def test_wire_non_v3_preview_is_refused(
    controlled_session: Mock,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tests.test_billing_opening_export_cli import preview as v1_preview

    _patch(monkeypatch, v1_preview())
    assert cli.main(_args("--output", "wire")) == 2
    assert json.loads(capsys.readouterr().out)["error"] == (
        "evidence_serialization_failed"
    )
