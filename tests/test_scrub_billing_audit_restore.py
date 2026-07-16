from __future__ import annotations

import ast
from pathlib import Path

from scripts.one_off import scrub_billing_audit_restore as scrub


def test_retired_scrubber_fails_before_database_access(capsys) -> None:
    assert scrub.main() == 2
    assert "retired" in capsys.readouterr().err


def test_retired_scrubber_has_no_database_capability() -> None:
    source = Path(scrub.__file__).read_text(encoding="utf-8")
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert imported_roots.isdisjoint({"app", "psycopg", "sqlalchemy"})
