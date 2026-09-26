"""``EnforcementApplication`` has exactly one writer (ADR-0017 §3, §7).

The model's own module docstring
(``app/models/enforcement_application.py``) states
``app.services.enforcement`` is the sole writer, and the writer function's
docstring (``_record_enforcement_application``) repeats the same claim. A
second writer would race the out-of-band upsert this function performs
specifically to survive the caller's transaction, so a second writer is a
correctness bug, not a style preference.

This scans every module under ``app/`` (excluding ``app/models/`` — the model
definition necessarily names its own class — and ``alembic/``, which the scan
never reaches since it is a sibling of ``app/``) for three ways a second
writer could appear: constructing ``EnforcementApplication(...)`` directly,
passing the class or its ``__table__`` to a Core ``insert``/``update``/
``delete``, or hand-writing the table name in a ``text()``/``execute()`` raw
SQL string.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OWNER_MODULE = "app/services/enforcement.py"

_TARGET_CLASS = "EnforcementApplication"
_TABLE_LITERAL = "enforcement_applications"
_DML_CALL_NAMES = {
    "insert",
    "update",
    "delete",
    "pg_insert",
    "sqlite_insert",
    "mysql_insert",
    "bulk_insert_mappings",
    "bulk_update_mappings",
    "bulk_save_objects",
}
_RAW_SQL_CALL_NAMES = {"text", "execute"}


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_dml_call(call: ast.Call) -> bool:
    name = _call_name(call)
    return name is not None and (name in _DML_CALL_NAMES or name.endswith("_insert"))


class _ModuleAliases:
    """Names that denote the model class or its table in one module.

    Resolves ``import ... as X`` and ``t = <class>.__table__`` so aliasing or
    one level of indirection cannot hide a second writer.
    """

    def __init__(self, tree: ast.AST) -> None:
        self.classes: set[str] = {_TARGET_CLASS}
        self.tables: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == _TARGET_CLASS:
                        self.classes.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and self.is_table(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.tables.add(target.id)

    def is_class(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.classes
        # Module-qualified access: `em.EnforcementApplication` after
        # `import app.models.enforcement_application as em`.
        return isinstance(node, ast.Attribute) and node.attr == _TARGET_CLASS

    def is_table(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name) and node.id in self.tables:
            return True
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "__table__"
            and self.is_class(node.value)
        )

    def references_model(self, node: ast.expr) -> bool:
        return self.is_class(node) or self.is_table(node)


def _string_constants(node: ast.expr) -> list[str]:
    """Extract literal string pieces from a plain or f-string argument."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [
            value.value
            for value in node.values
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        ]
    return []


def _raw_sql_names_the_table(call: ast.Call) -> bool:
    if _call_name(call) not in _RAW_SQL_CALL_NAMES:
        return False
    for arg in call.args:
        for literal in _string_constants(arg):
            if _TABLE_LITERAL in literal:
                return True
    return False


def _is_offending_call(call: ast.Call, aliases: _ModuleAliases) -> bool:
    if aliases.is_class(call.func):
        return True  # constructing the model
    if _is_dml_call(call):
        arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
        if any(aliases.references_model(arg) for arg in arguments):
            return True
    return _raw_sql_names_the_table(call)


def _module_is_offender(tree: ast.AST) -> bool:
    aliases = _ModuleAliases(tree)
    return any(
        isinstance(node, ast.Call) and _is_offending_call(node, aliases)
        for node in ast.walk(tree)
    )


def _excluded(relative_path: str) -> bool:
    parts = relative_path.split("/")
    return "models" in parts or "alembic" in parts


def find_offending_modules(repo_root: Path, *, owner: str = OWNER_MODULE) -> set[str]:
    """Every module under ``repo_root/app`` that writes ``EnforcementApplication``
    outside the declared owner. Public so the sensitivity test below can
    exercise it directly against a planted fixture tree."""
    app_dir = repo_root / "app"
    offenders: set[str] = set()
    if not app_dir.is_dir():
        return offenders
    for path in sorted(app_dir.rglob("*.py")):
        relative_path = path.relative_to(repo_root).as_posix()
        if _excluded(relative_path) or relative_path == owner:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover - a parse failure is a lint problem
            continue
        if _module_is_offender(tree):
            offenders.add(relative_path)
    return offenders


def test_no_offenders_write_enforcement_application_outside_its_owner() -> None:
    offenders = find_offending_modules(ROOT)
    assert not offenders, (
        "These modules write EnforcementApplication outside its sole owner "
        f"({OWNER_MODULE}):\n  "
        + "\n  ".join(sorted(offenders))
        + "\n\nRoute the write through app.services.enforcement's "
        "_record_enforcement_application (or add a real new capability there) "
        "instead — a second writer races the out-of-band upsert that exists "
        "specifically to survive the caller's own transaction (ADR-0017 §3)."
    )


class TestScannerSensitivity:
    """A guard that never fires proves nothing (ADR-0018). Plant a genuine
    offender and a near-miss against the SAME scanner function used above."""

    def test_a_planted_offender_is_flagged(self, tmp_path: Path) -> None:
        services_dir = tmp_path / "app" / "services"
        services_dir.mkdir(parents=True)

        # Owner module: also constructs the class, but must be excluded by name.
        (services_dir / "enforcement.py").write_text(
            "class EnforcementApplication:\n"
            "    pass\n\n"
            "def _record_enforcement_application():\n"
            "    return EnforcementApplication()\n"
        )

        # Planted offender: a second writer constructing the row directly.
        (services_dir / "bad_module.py").write_text(
            "from app.models.enforcement_application import EnforcementApplication\n\n"
            "def sneaky_write(db):\n"
            "    db.add(EnforcementApplication(subscription_id=1))\n"
        )

        # A second offender shape: raw insert() against the table.
        (services_dir / "bad_module_dml.py").write_text(
            "from sqlalchemy import insert\n"
            "from app.models.enforcement_application import EnforcementApplication\n\n"
            "def sneaky_upsert(session):\n"
            "    session.execute(insert(EnforcementApplication.__table__).values())\n"
        )

        # A third offender shape: raw SQL naming the table.
        (services_dir / "bad_module_sql.py").write_text(
            "from sqlalchemy import text\n\n"
            "def sneaky_sql(session):\n"
            '    session.execute(text("DELETE FROM enforcement_applications"))\n'
        )

        # Aliased import: `as EA` must not hide construction.
        (services_dir / "bad_module_alias.py").write_text(
            "from app.models.enforcement_application import "
            "EnforcementApplication as EA\n\n"
            "def sneaky_alias(db):\n"
            "    db.add(EA(subscription_id=1))\n"
        )

        # The owner's own shape copied elsewhere: a dialect insert on a
        # table bound through a variable.
        (services_dir / "bad_module_dialect.py").write_text(
            "from sqlalchemy.dialects.postgresql import insert as pg_insert\n"
            "from app.models.enforcement_application import EnforcementApplication\n\n"
            "def sneaky_upsert(session):\n"
            "    table = EnforcementApplication.__table__\n"
            "    session.execute(pg_insert(table).values())\n"
        )

        # ORM bulk write.
        (services_dir / "bad_module_bulk.py").write_text(
            "from app.models.enforcement_application import EnforcementApplication\n\n"
            "def sneaky_bulk(session):\n"
            "    session.bulk_insert_mappings(EnforcementApplication, [{}])\n"
        )

        # Module-qualified construction through a module alias.
        (services_dir / "bad_module_qualified.py").write_text(
            "import app.models.enforcement_application as em\n\n"
            "def sneaky_qualified(db):\n"
            "    db.add(em.EnforcementApplication(subscription_id=1))\n"
        )

        models_dir = tmp_path / "app" / "models"
        models_dir.mkdir(parents=True)
        (models_dir / "enforcement_application.py").write_text(
            "class EnforcementApplication:\n    pass\n"
        )

        offenders = find_offending_modules(tmp_path)

        assert "app/services/bad_module.py" in offenders
        assert "app/services/bad_module_dml.py" in offenders
        assert "app/services/bad_module_sql.py" in offenders
        assert "app/services/bad_module_alias.py" in offenders
        assert "app/services/bad_module_dialect.py" in offenders
        assert "app/services/bad_module_bulk.py" in offenders
        assert "app/services/bad_module_qualified.py" in offenders
        assert "app/services/enforcement.py" not in offenders
        assert "app/models/enforcement_application.py" not in offenders

    def test_a_near_miss_is_not_flagged(self, tmp_path: Path) -> None:
        services_dir = tmp_path / "app" / "services"
        services_dir.mkdir(parents=True)
        (services_dir / "enforcement.py").write_text(
            "class EnforcementApplication:\n    pass\n"
        )

        # Referencing the class (import, attribute access, type hint) is not
        # a write — only construction/DML/raw-SQL naming the table is.
        (services_dir / "reader_module.py").write_text(
            "from app.models.enforcement_application import EnforcementApplication\n\n"
            "def read_table():\n"
            "    table = EnforcementApplication.__table__\n"
            "    return table\n\n"
            "def annotate(value: EnforcementApplication) -> None:\n"
            "    return None\n"
        )

        offenders = find_offending_modules(tmp_path)

        assert offenders == set()
