"""Runtime audit mutation guard, independent of the retired soft-delete shape.

Static provenance follows imports, local aliases and ORM query results. Dynamic
imports/reflection, cross-function flows and generated SQL need independent review;
this is source governance, not database enforcement. Historical migrations and
explicit disposable rehearsal writers are inventoried in the boundary document.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def audit_mutations(source: str) -> set[int]:
    tree = ast.parse(source)
    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    lines: set[int] = set()
    scopes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    # Analyze functions independently so a query variable named row in one
    # function cannot taint another function's unrelated row.
    for scope in scopes:
        body = [*imports, *scope.body]
        for argument in [
            *scope.args.posonlyargs,
            *scope.args.args,
            *scope.args.kwonlyargs,
        ]:
            if argument.annotation:
                body.append(
                    ast.AnnAssign(
                        target=ast.Name(id=argument.arg, ctx=ast.Store()),
                        annotation=argument.annotation,
                        value=None,
                        simple=1,
                    )
                )
        lines.update(_scope_mutations(ast.Module(body=body, type_ignores=[])))
    lines.update(_scope_mutations(tree))
    return lines


def _scope_mutations(tree: ast.AST) -> set[int]:
    def scope_nodes(node: ast.AST):
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield from scope_nodes(child)

    nodes = list(scope_nodes(tree))
    audit_names = {"AuditEvent", "audit_events"}
    sql_functions = {"update", "delete"}
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "AuditEvent":
                    audit_names.add(alias.asname or alias.name)
                if alias.name in {"update", "delete"}:
                    sql_functions.add(alias.asname or alias.name)

    def refers(node: ast.AST) -> bool:
        return any(
            isinstance(child, ast.Name)
            and child.id in audit_names
            or isinstance(child, ast.Attribute)
            and child.attr in audit_names
            or isinstance(child, ast.Constant)
            and child.value == "audit_events"
            for child in ast.walk(node)
        )

    # Fixed point handles aliases declared after use and query/row variables.
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value:
                if refers(node.value) and not (
                    isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id
                    in {"dict", "str", "int", "bool", "float", "Decimal"}
                ):
                    targets = (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                    for target in targets:
                        if (
                            isinstance(target, ast.Name)
                            and target.id not in audit_names
                        ):
                            audit_names.add(target.id)
                            changed = True
            if isinstance(node, ast.For) and refers(node.iter):
                if (
                    isinstance(node.target, ast.Name)
                    and node.target.id not in audit_names
                ):
                    audit_names.add(node.target.id)
                    changed = True
            if isinstance(node, ast.AnnAssign) and refers(node.annotation):
                if (
                    isinstance(node.target, ast.Name)
                    and node.target.id not in audit_names
                ):
                    audit_names.add(node.target.id)
                    changed = True

    offenders: set[int] = set()
    for node in nodes:
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)):
            targets = (
                node.targets
                if isinstance(node, (ast.Assign, ast.Delete))
                else [node.target]
            )
            if any(
                isinstance(target, (ast.Attribute, ast.Subscript))
                and refers(target.value)
                for target in targets
            ):
                offenders.add(node.lineno)
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else getattr(node.func, "id", "")
        )
        if name in sql_functions | {
            "bulk_update_mappings",
            "bulk_save_objects",
        } and refers(node):
            offenders.add(node.lineno)
        if name in {"setattr", "delattr"} and node.args and refers(node.args[0]):
            offenders.add(node.lineno)
        if (
            name in {"pop", "clear", "update", "__setitem__", "__delitem__"}
            and isinstance(node.func, ast.Attribute)
            and refers(node.func.value)
        ):
            offenders.add(node.lineno)
        for arg in node.args:
            if (
                isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
                and re.search(
                    r"\b(?:UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?)\s+"
                    r'(?:"?\w+"?\s*\.\s*)?"?audit_events(?!\w)"?',
                    arg.value,
                    re.IGNORECASE,
                )
            ):
                offenders.add(node.lineno)
    return offenders


def test_runtime_audit_mutation_inventory_cannot_grow() -> None:
    offenders = {
        str(path.relative_to(ROOT)): sorted(lines)
        for path in sorted((ROOT / "app").rglob("*.py"))
        if (lines := audit_mutations(path.read_text()))
    }
    assert not offenders, (
        f"Runtime audit mutation inventory must stay empty: {offenders}"
    )


@pytest.mark.parametrize(
    "source",
    [
        "event = db.get(AuditEvent, key)\nevent.is_active = False",
        "from app.models.audit import AuditEvent as Evidence\ne = db.get(Evidence, key)\ne.action = 'rewrite'",
        "db.delete(db.get(AuditEvent, key))",
        "rows = db.query(AuditEvent).all()\nfor row in rows:\n row.action = 'rewrite'",
        "db.query(AuditEvent).filter(AuditEvent.id == key).update({'is_active': False})",
        "db.query(AuditEvent).delete()",
        "from sqlalchemy import update as rewrite\ndb.execute(rewrite(AuditEvent).values(action='rewrite'))",
        "db.execute(sa.delete(AuditEvent.__table__))",
        "audit = AuditEvent.__table__\ndb.execute(audit.update().values(action='rewrite'))",
        "db.bulk_update_mappings(AuditEvent, mappings)",
        "def rewrite(event: AuditEvent):\n setattr(event, 'action', 'rewrite')",
        "event = db.get(AuditEvent, key)\nevent.details.update({'changed': True})",
        "db.execute(text('UPDATE public.audit_events SET is_active = false'))",
        "db.execute(text('DELETE FROM audit_events'))",
        "db.execute(text('TRUNCATE TABLE audit_events'))",
        "event = db.get(AuditEvent, key)\ndel event.details['x']",
        "event = db.get(AuditEvent, key)\ndel event.metadata_['nested']['x']",
        "event = db.get(AuditEvent, key)\ndel event.actor_label",
        "db.execute(text('TRUNCATE public.audit_events'))",
        'db.execute(text(\'TRUNCATE TABLE "public"."audit_events"\'))',
        'db.execute(text(\'TRUNCATE "public" . "audit_events" CASCADE\'))',
    ],
)
def test_detector_covers_orm_core_and_sql_mutation_shapes(source: str) -> None:
    assert audit_mutations(source)


def test_detector_allows_canonical_append_and_historical_filters() -> None:
    assert not audit_mutations("""
event = AuditEvent(**data)
db.add(event)
stmt = select(AuditEvent).where(AuditEvent.is_active == False)
rows = db.scalars(stmt).all()
db.execute(insert(AuditEvent).values(action='append'))
""")


@pytest.mark.parametrize(
    "source",
    [
        "del other.details['x']",
        "event = db.get(OtherEvent, key)\ndel event.metadata_['nested']['x']",
        "db.execute(text('TRUNCATE public.other_events'))",
        'db.execute(text(\'TRUNCATE TABLE "public"."audit_events_archive"\'))',
        "db.execute(text('TRUNCATE public.audit_events_backup'))",
        "db.execute(text('SELECT * FROM public.audit_events'))",
    ],
)
def test_detector_distinguishes_unrelated_deletes_and_sql(source: str) -> None:
    assert not audit_mutations(source)
