from __future__ import annotations

import ast
import csv
import stat
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.one_off.billing_alignment_audit import (
    EVIDENCE_SCHEMAS,
    Finding,
    _write_csv,
)


def test_every_detector_row_shape_is_registered() -> None:
    source_path = Path("scripts/one_off/billing_alignment_audit.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    found: dict[str, set[tuple[str, ...]]] = {}
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef):
            continue
        code = function.name.split("_", 1)[0].upper()
        if code not in EVIDENCE_SCHEMAS:
            continue
        for node in ast.walk(function):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and node.args
                and isinstance(node.args[0], ast.Dict)
            ):
                continue
            keys = tuple(
                key.value
                for key in node.args[0].keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            )
            if len(keys) == len(node.args[0].keys):
                found.setdefault(code, set()).add(keys)

    assert found
    assert found == {code: set(schemas) for code, schemas in EVIDENCE_SCHEMAS.items()}


def test_portable_evidence_schema_excludes_sensitive_and_free_text_fields() -> None:
    forbidden = {
        "address",
        "body",
        "config",
        "description",
        "email",
        "external_id",
        "headers",
        "invoice_number",
        "memo",
        "metadata",
        "name",
        "notes",
        "payload",
        "phone",
        "recipient",
        "secret",
        "token",
    }
    fields = {
        field
        for schemas in EVIDENCE_SCHEMAS.values()
        for schema in schemas
        for field in schema
    }
    assert not fields.intersection(forbidden)


def test_writer_rejects_an_unclassified_field_before_creating_output(tmp_path) -> None:
    finding = Finding("D12", "Enforcement mismatch", "F6/F7")
    finding.rows.append(
        {
            "account_id": "00000000-0000-0000-0000-000000000001",
            "available": "0.00",
            "threshold": "5000.00",
            "locked": False,
            "served": True,
            "verdict": "unfunded_and_served",
            "recipient": "must-not-leave",
        }
    )

    with pytest.raises(ValueError, match="not allowlisted"):
        _write_csv(tmp_path / "evidence", finding)
    assert not (tmp_path / "evidence").exists()


def test_writer_creates_new_private_allowlisted_evidence(tmp_path) -> None:
    finding = Finding("D12", "Enforcement mismatch", "F6/F7")
    finding.amount = Decimal("5000.00")
    finding.rows.append(
        {
            "account_id": "00000000-0000-0000-0000-000000000001",
            "available": "0.00",
            "threshold": "5000.00",
            "locked": False,
            "served": True,
            "verdict": "unfunded_and_served",
        }
    )

    path = _write_csv(tmp_path / "evidence", finding)

    assert path is not None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0]) == list(EVIDENCE_SCHEMAS["D12"][0])
    assert rows[0]["account_id"].endswith("0001")
    with pytest.raises(FileExistsError):
        _write_csv(tmp_path / "evidence", finding)


def test_writer_rejects_unregistered_detector(tmp_path) -> None:
    finding = Finding("D99", "New detector", "--")
    finding.rows.append({"account_id": "id"})
    with pytest.raises(ValueError, match="no portable-evidence schema"):
        _write_csv(tmp_path, finding)
