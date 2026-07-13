"""One owner decides who we may contact. Keep it that way.

The disease this cures: marketing eligibility was decided inside the campaign
segment filter, where opting in was an optional checkbox. So the answer to "may
we email this person" depended on which sender you asked -- and a customer who
unsubscribed from one path stayed reachable by every other.

A rule with two implementations has no owner. These tests fail if a second one
appears.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: The ONE module allowed to decide contact eligibility.
OWNER = "app/services/communication_eligibility.py"

#: The ONE place transports are invoked, and therefore the only place the gate
#: is guaranteed to run. A sender that calls a transport from anywhere else can
#: skip the ledger -- which is the bug, not a style issue.
DELIVERY_POINT = "app/tasks/notifications.py"

TRANSPORT_CALLS = {
    "send_email",
    "send_sms",
    "send_push",
    "send_text_message",
}

#: Pre-existing direct-transport callers. This list must only ever SHRINK --
#: each one is a path that can bypass the consent ledger. It is not an
#: allowlist of "fine"; it is a to-do list.
KNOWN_DIRECT_SENDERS = {
    "app/services/email.py",  # the transport itself
    "app/services/sms.py",
    "app/services/push.py",
}


def _py_files() -> list[Path]:
    return [
        p
        for p in (ROOT / "app").rglob("*.py")
        if "__pycache__" not in p.parts
    ]


def test_the_consent_rule_has_exactly_one_implementation() -> None:
    """`may_send` / `is_marketing` must be defined once, in the owner."""
    definitions: dict[str, list[str]] = {"may_send": [], "is_marketing": []}

    for path in _py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        rel = str(path.relative_to(ROOT))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in definitions:
                    definitions[node.name].append(rel)

    for name, found in definitions.items():
        assert found == [OWNER], (
            f"`{name}` must be defined only in {OWNER}, found in: {found}. "
            "A second implementation of the consent rule means the answer "
            "depends on who is asking -- which is the bug this ledger exists "
            "to remove."
        )


def test_no_new_module_calls_a_transport_directly() -> None:
    """Every send must pass the gate in the delivery point.

    A module that calls `send_email` itself can mail an unsubscribed customer
    without ever consulting the ledger.
    """
    offenders: list[str] = []

    for path in _py_files():
        rel = str(path.relative_to(ROOT))
        if rel in KNOWN_DIRECT_SENDERS or rel == DELIVERY_POINT:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if name in TRANSPORT_CALLS:
                offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, (
        "These call a transport directly, bypassing the consent ledger:\n  "
        + "\n  ".join(sorted(offenders))
        + f"\n\nSubmit through {DELIVERY_POINT}, which gates on "
        "communication_eligibility.may_send(). If a path genuinely must send "
        "directly, add it to KNOWN_DIRECT_SENDERS *and say why* -- that set is "
        "a to-do list, not an allowlist."
    )


def test_the_delivery_point_actually_consults_the_ledger() -> None:
    """A gate that is not wired in is worse than no gate: it looks safe."""
    source = (ROOT / DELIVERY_POINT).read_text(encoding="utf-8")
    assert "communication_eligibility" in source, (
        f"{DELIVERY_POINT} must import the eligibility owner"
    )
    assert "may_send(" in source, (
        f"{DELIVERY_POINT} must gate every send on may_send() -- it is the only "
        "place all four transports are called, so it is the only place the "
        "check is guaranteed to run."
    )
