"""Audit rows are written through two sanctioned surfaces only.

The audit table has one model (`AuditEvent`) and, as of this pin, exactly two
sanctioned writer surfaces with distinct transaction semantics:

- ``record_audit_event`` (``app/services/audit_adapter.py``) — the keyword
  facade for request/consequence paths; supports defer-until-commit via
  ``AuditEvents.record`` underneath.
- ``AuditEvents.stage`` (``app/services/audit.py``) — stages the row in the
  CALLER'S current transaction without committing; the correct surface for
  services that own their transaction outcome (billing/payments use this
  heavily and deliberately).

``AuditEvents.create`` (commits immediately) and direct ``AuditEvents.record``
calls outside the adapter are banned: an immediate commit inside another
owner's transaction is exactly the premature-commit class of bug the
transaction-ownership contract exists to prevent. At the time this pin
landed there were ZERO such callers — this test keeps it that way rather
than migrating anything.

Detection is AST-based: any ``Attribute`` call ``AuditEvents.create(...)`` /
``AuditEvents.record(...)`` in ``app/`` outside the two owner modules fails.
Aliasing (``x = AuditEvents; x.create(...)``) evades the walker — disclosed
limit, consistent with the repo's other AST governance tests.
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"

OWNER_MODULES = {
    "app/services/audit.py",
    "app/services/audit_adapter.py",
}
_BANNED_METHODS = {"create", "record"}


def test_no_direct_audit_create_or_record_outside_owners() -> None:
    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if rel in OWNER_MODULES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — syntax is checked elsewhere
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _BANNED_METHODS
                and isinstance(func.value, ast.Name)
                and func.value.id == "AuditEvents"
            ):
                offenders.append(f"{rel}:{node.lineno} AuditEvents.{func.attr}")
    assert not offenders, (
        "Direct AuditEvents.create/.record outside the audit owners — use "
        "record_audit_event (app/services/audit_adapter.py) for request/"
        "consequence paths, or AuditEvents.stage inside a transaction-owning "
        "service: " + ", ".join(sorted(offenders))
    )
