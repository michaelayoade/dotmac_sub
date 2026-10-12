"""Policy 2026-10-12: --apply is refused on metadata-keyed prepaid repair scripts."""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from scripts.billing import _mutation_policy

_UUID = "00000000-0000-0000-0000-000000000001"
_TS = "2026-01-01T00:00:00+00:00"

_COMMON = [
    "--account-id",
    _UUID,
    "--subscription-id",
    _UUID,
]
_CASES = {
    "correct_tax_inclusive_renewal_document": [
        *_COMMON,
        "--adjustment-id",
        _UUID,
        "--entitlement-id",
        _UUID,
        "--payment-id",
        _UUID,
        "--expected-invoice-total",
        "10.00",
        "--expected-remaining-credit",
        "0.00",
    ],
    "repair_legacy_payment_backed_renewal_invoice": None,
    "replace_reviewed_paid_prepaid_period": None,
}


def _required_args(module, name: str) -> list[str]:
    """Build the minimal required argv by reading the parser's required options."""
    argv = _CASES[name]
    if argv is not None:
        return list(argv)
    captured: dict[str, object] = {}

    class _Stop(Exception):
        pass

    import argparse

    original = argparse.ArgumentParser.parse_args

    def _capture(self, *a, **k):
        captured["parser"] = self
        raise _Stop

    argparse.ArgumentParser.parse_args = _capture  # type: ignore[method-assign]
    try:
        with pytest.raises(_Stop):
            module.main()
    finally:
        argparse.ArgumentParser.parse_args = original  # type: ignore[method-assign]
    parser = captured["parser"]
    out: list[str] = []
    for action in parser._actions:  # noqa: SLF001
        if not action.required:
            continue
        flag = action.option_strings[0]
        if action.type is None:
            value = "x"
        else:
            probe = {
                "_uuid": _UUID,
                "_money": "10.00",
                "_timestamp": _TS,
                "_aware_datetime": _TS,
            }
            value = probe.get(getattr(action.type, "__name__", ""), _UUID)
        out.extend([flag, value])
    return out


@pytest.fixture(params=list(_CASES))
def script(request, monkeypatch):
    name = request.param
    module = importlib.import_module(f"scripts.billing.{name}")
    adapter = MagicMock()
    read_db = MagicMock()

    @contextmanager
    def read_session():
        yield read_db

    adapter.read_session = read_session
    monkeypatch.setattr(module, "db_session_adapter", adapter)
    return name, module, adapter


def test_apply_is_refused_without_touching_the_database(script, monkeypatch, capsys):
    name, module, adapter = script
    argv = _required_args(module, name) + ["--apply"]
    monkeypatch.setattr("sys.argv", [name, *argv])

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert exc.value.code not in (0, None)
    err = capsys.readouterr().err
    assert "billing repair writes are disabled by policy (2026-10-12)" in err
    assert "billing exceptions review" in err
    assert adapter.method_calls == []  # no session of any kind was requested


def test_apply_with_full_evidence_is_still_refused(script, monkeypatch, capsys):
    name, module, adapter = script
    argv = _required_args(module, name) + [
        "--apply",
        "--idempotency-key",
        "abcdefghijklmnop1234",
    ]
    monkeypatch.setattr("sys.argv", [name, *argv])

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert exc.value.code not in (0, None)
    assert "disabled by policy" in capsys.readouterr().err
    assert adapter.method_calls == []


def test_preview_still_runs(script, monkeypatch):
    name, module, _adapter = script
    previewer = next(
        attr
        for attr in dir(module)
        if attr.startswith("preview_") and callable(getattr(module, attr))
    )
    sentinel = MagicMock(side_effect=RuntimeError("preview reached"))
    monkeypatch.setattr(module, previewer, sentinel)
    monkeypatch.setattr("sys.argv", [name, *_required_args(module, name)])

    with pytest.raises(RuntimeError, match="preview reached"):
        module.main()

    sentinel.assert_called_once()


def test_guard_allows_non_apply(capsys):
    _mutation_policy.refuse_apply(False)
    assert capsys.readouterr().err == ""
