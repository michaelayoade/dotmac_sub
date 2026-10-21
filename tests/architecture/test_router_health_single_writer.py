"""Single-writer guards for the router health tables (ADR 0018).

* ``RouterHealthSignal`` / ``router_health_signals``: written only inside
  ``app/services/router_health/`` (its owner commands).
* ``NasCoaOutcomeBucket`` / ``nas_coa_outcome_buckets``: written only by
  ``app/services/router_health/coa_evidence.py`` (the out-of-band writer).

A write is constructing the model, passing it (or its ``__table__``) to a DML
call, or hand-written SQL naming the table. Reads (``select``) are allowed.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "app/services/router_health/"
BUCKET_WRITER = "app/services/router_health/coa_evidence.py"

_DML = {
    "insert",
    "update",
    "delete",
    "pg_insert",
    "sqlite_insert",
    "add",
    "add_all",
    "merge",
}
_RAW = {"text", "execute"}


def _writes(source: str, class_name: str, table: str) -> list[int]:
    tree = ast.parse(source)
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == class_name:
            hits.append(node.lineno)
        if name in _DML:
            for arg in ast.walk(node):
                if isinstance(arg, ast.Name) and arg.id == class_name:
                    hits.append(node.lineno)
                    break
                if isinstance(arg, ast.Attribute) and arg.attr == "__table__":
                    inner = arg.value
                    if isinstance(inner, ast.Name) and inner.id == class_name:
                        hits.append(node.lineno)
                        break
        if name in _RAW:
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if table in arg.value:
                        hits.append(node.lineno)
    return hits


def _scan(class_name: str, table: str, allowed) -> list[str]:
    offenders: list[str] = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("app/models/") or allowed(rel):
            continue
        for line in _writes(path.read_text(encoding="utf-8"), class_name, table):
            offenders.append(f"{rel}:{line}")
    return offenders


def test_router_health_signals_have_a_single_writer_package() -> None:
    offenders = _scan(
        "RouterHealthSignal",
        "router_health_signals",
        lambda rel: rel.startswith(PACKAGE),
    )
    assert not offenders, offenders


def test_coa_buckets_have_a_single_writer_module() -> None:
    offenders = _scan(
        "NasCoaOutcomeBucket",
        "nas_coa_outcome_buckets",
        lambda rel: rel == BUCKET_WRITER,
    )
    assert not offenders, offenders


def test_the_writers_do_write_so_the_scan_is_not_vacuous() -> None:
    commands = (ROOT / PACKAGE / "commands.py").read_text()
    assert _writes(commands, "RouterHealthSignal", "router_health_signals")
    writer = (ROOT / BUCKET_WRITER).read_text()
    assert _writes(writer, "NasCoaOutcomeBucket", "nas_coa_outcome_buckets")


def test_scanner_flags_planted_writers() -> None:
    planted = "from x import NasCoaOutcomeBucket\ndb.add(NasCoaOutcomeBucket(a=1))\n"
    assert _writes(planted, "NasCoaOutcomeBucket", "nas_coa_outcome_buckets")
    raw = 'db.execute(text("DELETE FROM nas_coa_outcome_buckets"))\n'
    assert _writes(raw, "NasCoaOutcomeBucket", "nas_coa_outcome_buckets")
    read = "db.execute(select(NasCoaOutcomeBucket.count))\n"
    assert not _writes(read, "NasCoaOutcomeBucket", "nas_coa_outcome_buckets")
