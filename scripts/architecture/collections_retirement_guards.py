"""Inventory the duplicate Sub authorities that Collections must retire.

This detector is evidence, not a cutover switch.  It freezes the legacy
postpaid/prepaid writers and their displaced state while ``dotmac-collections``
shadows them.  The paired architecture test is two-directional: debt may not
grow, and a removal must lower the checked-in baseline in the same change.

The scanner is syntax-based and accepts a project root so its sensitivity test
can plant every defect family in a disposable tree.  It never imports the
application, opens a database or executes a task.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

LEGACY_CLASS_NAMES = frozenset(
    {
        "BillingEnforcementReconciler",
        "CollectionsCase",
        "CollectionsLifecycle",
        "DunningActionLog",
        "DunningCase",
        "DunningWorkflow",
        "FinancialAccessConsequence",
        "FinancialAccessConsequenceEvidence",
        "PaymentArrangement",
        "PaymentArrangementInstallment",
        "PaymentArrangementInstallments",
        "PaymentArrangements",
        "PrepaidSweepCycleState",
    }
)

LEGACY_FUNCTION_NAMES = frozenset(
    {
        "_execute_dunning_action",
        "_execute_dunning_action_with_evidence",
        "_restore_prepaid_if_funded",
        "_restore_throttle",
        "_suspend_account",
        "_throttle_account",
        "confirm_financial_access_consequence",
        "confirm_financial_access_restoration",
        "confirm_financial_access_restoration_for_owner",
        "prepaid_balance_sweep",
        "preview_financial_access_consequence",
        "preview_financial_access_restoration",
        "restore_account_services",
        "restore_account_services_detailed",
        "run_billing_enforcement",
        "run_prepaid_balance_sweep",
    }
)

LEGACY_TABLE_NAMES = frozenset(
    {
        "collections_cases",
        "dunning_action_logs",
        "dunning_cases",
        "financial_access_consequence_evidence",
        "financial_access_consequences",
        "payment_arrangement_installments",
        "payment_arrangements",
        "prepaid_sweep_cycle_state",
    }
)

LEGACY_SCHEDULE_NAMES = frozenset({"dunning_runner", "prepaid_balance_sweep"})

DIRECT_FINANCE_CALLS = frozenset(
    {
        "apply_prepaid_overlap_hold",
        "mark_overdue_system",
    }
)

DIRECT_ACCESS_OWNER_CALLS = frozenset(
    {
        "restore_subscription_detailed",
        "suspend_subscription",
    }
)

LEGACY_ACCESS_CALL_NAMES = frozenset(
    {
        "confirm_financial_access_consequence",
        "confirm_financial_access_restoration",
        "confirm_financial_access_restoration_for_owner",
        "preview_financial_access_consequence",
        "preview_financial_access_restoration",
        "restore_account_services",
        "restore_account_services_detailed",
    }
)

NOTICE_DELIVERY_CALLS = frozenset({"queue_customer_notification"})

AMBIENT_CLOCK_CALLS = frozenset(
    {
        "datetime.date.today",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "time.monotonic",
        "time.time",
    }
)

CREDENTIAL_WRITE_ATTRIBUTES = frozenset(
    {
        "pre_throttle_radius_profile_id",
        "radius_profile_id",
    }
)

PREPAID_TIMER_ATTRIBUTES = frozenset(
    {
        "prepaid_deactivation_at",
        "prepaid_low_balance_at",
    }
)

# Product lifecycle fields Collections must never start writing while the
# extraction is in flight.  There are intentionally no baseline entries: zero
# is the current allowance inside the legacy Collections roots.
PRODUCT_STATE_ATTRIBUTES = frozenset(
    {
        "access_state",
        "billing_enabled",
        "is_active",
        "status",
    }
)
PRODUCT_STATE_RECEIVERS = frozenset({"account", "subscriber", "subscription"})
CREDENTIAL_RECEIVERS = frozenset({"cred", "credential"})

RECEIVABLE_ANSWER_NAMES = frozenset(
    {
        "get_available_balance",
        "has_overdue_balance",
        "overdue_receivable_snapshot",
    }
)

COLLECTIONS_PACKAGE = "app.services.collections"
COLLECTIONS_PRIVATE_MODULE = f"{COLLECTIONS_PACKAGE}._core"


def _python_files(project_root: Path) -> list[Path]:
    paths: list[Path] = []
    for root_name in ("app", "scripts"):
        root = project_root / root_name
        if not root.exists():
            continue
        paths.extend(
            path
            for path in root.rglob("*.py")
            if path.is_file() and "__pycache__" not in path.parts
        )
    return sorted(paths)


def _relative(path: Path, project_root: Path) -> str:
    return path.relative_to(project_root).as_posix()


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _callee_name(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                aliases[local] = alias.name if alias.asname else local
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                aliases[local] = f"{node.module}.{alias.name}"
    return aliases


def _canonical_name(dotted: str, aliases: dict[str, str]) -> str:
    head, separator, tail = dotted.partition(".")
    target = aliases.get(head, head)
    return f"{target}.{tail}" if separator else target


def _attribute_receiver(node: ast.Attribute) -> str:
    value = node.value
    while isinstance(value, ast.Attribute):
        value = value.value
    return value.id if isinstance(value, ast.Name) else ""


def _assignment_targets(node: ast.AST) -> tuple[ast.expr, ...]:
    if isinstance(node, ast.Assign):
        return tuple(node.targets)
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        return (node.target,)
    return ()


def _is_collections_writer_path(relative: str) -> bool:
    return relative.startswith("app/services/collections/") or relative in {
        "app/services/payment_arrangements.py",
        "app/services/prepaid_enforcement_state.py",
        "app/tasks/collections.py",
    }


def _record_table_name(target: ast.expr, value: ast.expr, counts: Counter[str]) -> None:
    if not isinstance(target, ast.Name) or target.id != "__tablename__":
        return
    table = _literal_string(value)
    if table in LEGACY_TABLE_NAMES:
        counts[f"table:{table}"] += 1


def _record_schedule(node: ast.Call, counts: Counter[str]) -> None:
    if _callee_name(node.func) != "_sync_scheduled_task":
        return
    keywords = {item.arg: item.value for item in node.keywords if item.arg}
    name = _literal_string(keywords.get("name"))
    if name in LEGACY_SCHEDULE_NAMES:
        counts[f"schedule:{name}"] += 1


def _record_notice_subject(node: ast.AST, counts: Counter[str]) -> None:
    literal: str | None = None
    if isinstance(node, ast.Call):
        keyword = next((item for item in node.keywords if item.arg == "subject"), None)
        literal = _literal_string(keyword.value) if keyword else None
    elif (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Attribute)
        and node.left.attr == "subject"
    ):
        literal = next(
            (
                value
                for comparator in node.comparators
                if (value := _literal_string(comparator)) is not None
            ),
            None,
        )
    if literal is not None:
        counts[f"notice_subject_literal:{literal}"] += 1


def _record_import(
    node: ast.Import | ast.ImportFrom,
    *,
    relative: str,
    counts: Counter[str],
) -> None:
    # Imports inside the legacy package are implementation detail.  R11/R12
    # freeze external consumers, including the old flat compatibility shim.
    if relative.startswith("app/services/collections/"):
        return

    private = False
    aliases: tuple[ast.alias, ...]
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        aliases = tuple(node.names)
        if module == COLLECTIONS_PRIVATE_MODULE:
            private = True
        elif module == COLLECTIONS_PACKAGE and any(
            alias.name == "_core" or alias.name.startswith("_") for alias in aliases
        ):
            private = True
        if module == COLLECTIONS_PACKAGE or module == COLLECTIONS_PRIVATE_MODULE:
            for alias in aliases:
                if alias.name in RECEIVABLE_ANSWER_NAMES:
                    counts[f"receivable_answer_import:{alias.name}"] += 1
    else:
        aliases = tuple(node.names)
        private = any(alias.name == COLLECTIONS_PRIVATE_MODULE for alias in aliases)

    if private:
        counts[f"private_collections_import:{relative}"] += 1


def _record_assignment(
    target: ast.expr,
    *,
    collections_writer: bool,
    counts: Counter[str],
) -> None:
    if not isinstance(target, ast.Attribute):
        return
    receiver = _attribute_receiver(target)
    attribute = target.attr

    if attribute in PREPAID_TIMER_ATTRIBUTES:
        counts[f"prepaid_timer_write:{attribute}"] += 1

    if not collections_writer:
        return
    if receiver in CREDENTIAL_RECEIVERS and attribute in CREDENTIAL_WRITE_ATTRIBUTES:
        counts[f"credential_write:{attribute}"] += 1
    if receiver in PRODUCT_STATE_RECEIVERS and attribute in PRODUCT_STATE_ATTRIBUTES:
        counts[f"product_state_write:{receiver}.{attribute}"] += 1


def scan_collections_retirement_debt(
    project_root: Path = PROJECT_ROOT,
) -> dict[str, int]:
    """Return stable debt identities and their exact syntax-site counts."""

    counts: Counter[str] = Counter()
    if (project_root / "app/services/collections.py").is_file():
        counts["legacy_module_file:app/services/collections.py"] = 1
    for path in _python_files(project_root):
        relative = _relative(path, project_root)
        collections_writer = _is_collections_writer_path(relative)
        tree = _tree(path)
        aliases = _import_aliases(tree)

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in LEGACY_CLASS_NAMES:
                counts[f"class:{node.name}"] += 1
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in LEGACY_FUNCTION_NAMES:
                    counts[f"function:{node.name}"] += 1
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    _record_table_name(target, node.value, counts)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                _record_table_name(node.target, node.value, counts)

            for target in _assignment_targets(node):
                _record_assignment(
                    target,
                    collections_writer=collections_writer,
                    counts=counts,
                )

            if isinstance(node, (ast.Import, ast.ImportFrom)):
                _record_import(node, relative=relative, counts=counts)

            if collections_writer:
                _record_notice_subject(node, counts)

            if not isinstance(node, ast.Call):
                continue
            callee = _canonical_name(_dotted_name(node.func), aliases)
            callee_leaf = callee.rsplit(".", 1)[-1]
            if relative == "app/services/scheduler_config.py":
                _record_schedule(node, counts)
            if collections_writer and callee_leaf in DIRECT_FINANCE_CALLS:
                counts[f"finance_write_call:{callee_leaf}"] += 1
            if collections_writer and callee_leaf in DIRECT_ACCESS_OWNER_CALLS:
                counts[f"direct_access_owner_call:{callee_leaf}"] += 1
            if collections_writer and callee_leaf in NOTICE_DELIVERY_CALLS:
                counts[f"notice_delivery_call:{callee_leaf}"] += 1
            if collections_writer and callee in AMBIENT_CLOCK_CALLS:
                counts[f"ambient_clock_call:{callee}"] += 1

            external_collections_call = (
                not relative.startswith("app/services/collections/")
                and relative != "app/services/collections.py"
            )
            if not external_collections_call:
                continue
            if not callee.startswith(
                ("app.services.collections.", "app.services.collections._core.")
            ):
                continue
            if callee_leaf in RECEIVABLE_ANSWER_NAMES:
                counts[f"receivable_answer_call:{callee_leaf}"] += 1
            if callee_leaf in LEGACY_ACCESS_CALL_NAMES:
                counts[f"legacy_access_call:{callee_leaf}"] += 1

    return dict(sorted(counts.items()))


__all__ = [
    "PROJECT_ROOT",
    "scan_collections_retirement_debt",
]
