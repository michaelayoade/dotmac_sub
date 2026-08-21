"""The cohort export reads. It must be unable to do anything else.

`migration.cohort_export` runs against the production database of an
application that is still the sole authority for everything it reads. The
guarantee that matters is not "we were careful" — it is that the export path
contains no persistence call, no transaction completion, and no reference to a
destination's vocabulary, and that a reviewer can check that mechanically.

Each rejection is paired with an acceptance proof over a constructed example,
so a guard cannot pass because its detector stopped working.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.migration_source import snapshot
from tests.architecture.source_index import python_ast, python_nodes, source_text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPORT_SERVICE = PROJECT_ROOT / "app" / "services" / "migration_source_export.py"
EXPORT_CLI = PROJECT_ROOT / "scripts" / "migration" / "export_isp_cohort_snapshot.py"
CONTRACT_PACKAGE = PROJECT_ROOT / "app" / "migration_source"

#: Method calls that persist, complete a transaction, or discard one. A read
#: path needs none of them.
_MUTATING_CALLS = frozenset(
    {
        "add",
        "add_all",
        "bulk_insert_mappings",
        "bulk_save_objects",
        "bulk_update_mappings",
        "commit",
        "delete",
        "flush",
        "merge",
        "rollback",
    }
)

#: Statement builders that write. `select` is deliberately absent.
_DML_BUILDERS = frozenset({"insert", "update", "delete"})


def _mutating_calls(path: Path) -> list[str]:
    found: list[str] = []
    for node in python_nodes(path):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Attribute) and function.attr in _MUTATING_CALLS:
            found.append(f"{function.attr}() at line {node.lineno}")
        if isinstance(function, ast.Name) and function.id in _DML_BUILDERS:
            found.append(f"{function.id}() at line {node.lineno}")
    return sorted(found)


def _raw_dml(path: Path) -> list[str]:
    keywords = ("insert into", "update ", "delete from", "truncate", "alter ", "drop ")
    found: list[str] = []
    for node in python_nodes(path):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            lowered = node.value.lower()
            found.extend(
                f"{keyword.strip()!r} in a literal at line {node.lineno}"
                for keyword in keywords
                if keyword in lowered
            )
    return sorted(found)


def test_the_export_service_issues_no_persistence_call() -> None:
    offenders = _mutating_calls(EXPORT_SERVICE)
    assert not offenders, (
        "the cohort export runs against a production database it has no "
        "authority over. It may read and nothing else:\n  " + "\n  ".join(offenders)
    )


def test_the_export_service_issues_no_raw_dml() -> None:
    offenders = _raw_dml(EXPORT_SERVICE)
    assert not offenders, (
        "a write hidden in a SQL literal is still a write:\n  " + "\n  ".join(offenders)
    )


def test_the_detector_would_catch_a_write(tmp_path: Path) -> None:
    """The acceptance half. Without this the two guards above could pass
    because the AST walk silently stopped working."""

    planted = tmp_path / "planted.py"
    planted.write_text(
        "def save(db, row):\n"
        "    db.add(row)\n"
        "    db.commit()\n"
        "    db.execute('UPDATE subscribers SET is_active = false')\n",
        encoding="utf-8",
    )
    assert len(_mutating_calls(planted)) == 2
    assert _raw_dml(planted)


def test_the_export_service_pins_the_read_only_seam() -> None:
    body = source_text(EXPORT_SERVICE)
    assert "begin_read_only_snapshot" in body, (
        "the export must reach the repository's one read-only snapshot seam; "
        "a bespoke isolation setting here would be a second answer to a "
        "question app/db.py already owns"
    )


def test_both_adapters_open_the_read_only_session() -> None:
    assert "read_only_snapshot_session" in source_text(EXPORT_CLI), (
        "the operator entry point must open the read-only seam rather than an "
        "ordinary session; the service can only pin a session that has not "
        "already begun a transaction"
    )


def test_the_cli_delegates_and_decides_nothing() -> None:
    """A thin adapter: no queries, no model imports, no field mapping."""

    body = source_text(EXPORT_CLI)
    for forbidden in ("db.query(", "select(", "from app.models"):
        assert forbidden not in body, (
            f"{forbidden!r} in the export CLI. The owning service builds the "
            "snapshot so a second adapter cannot reach a different answer."
        )


def test_the_contract_package_holds_no_database_dependency() -> None:
    """The exported contract must be portable, and provably not a second reader.

    `app/migration_source/` describes the cohort; it never reaches a database.
    Keeping SQLAlchemy and `app.db` out of it is what lets the static writer
    census import the cohort declaration directly, and what stops the contract
    quietly growing a query of its own.
    """

    offenders: list[str] = []
    for path in sorted(CONTRACT_PACKAGE.glob("*.py")):
        for node in python_nodes(path):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in {"sqlalchemy", "psycopg", "asyncpg"} or (
                    name in {"app.db", "app.models"} or name.startswith("app.models.")
                ):
                    offenders.append(f"{path.name} imports {name}")
    assert not offenders, "\n  ".join(sorted(offenders))


def test_the_export_path_reaches_no_other_application() -> None:
    """No cross-application database, ORM or client may enter the read path.

    Applications compose by synchronising through versioned contracts. An
    export that could open the destination's database would be a second writer
    waiting for someone to pass it a DSN.
    """

    foreign_prefixes = (
        "dotmac_isp",
        "dotmac_crm",
        "dotmac_erp",
        "dotmac_integration",
        "dotmac_workspace",
        "dotmac_vendor",
    )
    offenders: list[str] = []
    for path in (EXPORT_SERVICE, EXPORT_CLI, *sorted(CONTRACT_PACKAGE.glob("*.py"))):
        for node in python_nodes(path):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders.extend(
                f"{path.name} imports {name}"
                for name in names
                if name.startswith(foreign_prefixes)
            )
    assert not offenders, "\n  ".join(sorted(offenders))


def test_the_export_service_declares_no_untyped_contract() -> None:
    """`Any` in a public signature would undo the typed-contract rule."""

    tree = python_ast(EXPORT_SERVICE)
    offenders = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and not node.name.startswith("_")
        and any(
            isinstance(annotation, ast.Name) and annotation.id == "Any"
            for annotation in [
                *(argument.annotation for argument in node.args.args),
                node.returns,
            ]
            if annotation is not None
        )
    ]
    assert not offenders, (
        "public export functions may not take or return `Any`: " + ", ".join(offenders)
    )


@pytest.mark.parametrize(
    "record_type",
    sorted(snapshot.RECORD_TYPES.values(), key=lambda item: item.__name__),
)
def test_every_record_type_is_frozen_and_closed(record_type: type) -> None:
    """A mutable or open record would let a consumer add an unversioned field."""

    config = record_type.model_config
    assert config.get("frozen") is True, record_type.__name__
    assert config.get("extra") == "forbid", record_type.__name__
