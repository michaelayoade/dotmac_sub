"""Static canaries for the billing repair CLI write-mode freeze."""

from __future__ import annotations

import argparse
import ast
import copy
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from scripts.billing._mutation_policy import REFUSAL_MESSAGE, refuse_apply

ROOT = Path(__file__).resolve().parents[1]
APPLY_CLIS = (
    "repair_prepaid_overlaps.py",
    "repair_native_prepaid_opening.py",
    "reconcile_account_credit_invoice.py",
    "reconstruct_reviewed_prepaid_invoice_sequence.py",
    "repair_prepaid_funding_consequences.py",
    "reconcile_paystack_reference.py",
    "recover_period_purchase.py",
    "settle_reviewed_prepaid_draft.py",
    "correct_historical_invoice_tax.py",
    "correct_historical_invoice_tax_using_existing_replacement.py",
    "apply_billing_remediation.py",
    "apply_billing_cleanup_remediation.py",
    "correct_reviewed_prepaid_sequence_funding.py",
    "prepaid_coverage_reconcile.py",
    "correct_customer_subledger_opening.py",
    "reconcile_prepaid_drafts.py",
)
EXECUTE_CLIS = (
    "backfill_ledger_effective_date.py",
    "purge_phantom_invoices.py",
    "purge_phantom_notifications.py",
)
ONE_OFF_APPLY_CLIS = (
    "soft_delete_failed_payment_reversal_pairs.py",
    "repair_billing_date_anchors.py",
    "repair_stale_prepaid_billing_anchors.py",
    "align_catalog_vat_flags.py",
    "repair_open_zero_vat_invoices.py",
    "backfill_payment_channel_attribution.py",
    "soft_delete_phantom_opening_balance_pairs.py",
    "materialize_prepaid_funding_reconstruction.py",
    "reverse_duplicate_paystack_recoveries.py",
    "reconcile_prepaid_drawdown_reversals.py",
    "reverse_erroneous_prepaid_drawdown.py",
    "reverse_erroneous_credit_settle.py",
    "reconcile_phantom_opening_debits.py",
    "cleanup_prepaid_phantom_ar.py",
    "cleanup_prepaid_phantom_invoices.py",
    "repair_splynx_overallocated_payments.py",
    "writeoff_terminal_arrears_cruft.py",
    "paystack_cutover_post_credits.py",
    "cutover_reconstructed_balance.py",
    "audit_phantom_opening_reversal_counterfactual.py",
    "audit_void_mirror_double_reversals.py",
    "reconcile_unposted_payments.py",
    "register_cutover_balance_variances.py",
)
SUBCOMMAND_WRITE_CLIS = (
    ("repair_prepaid_paid_invoice_period.py", "approve"),
    ("return_legacy_over_allocation.py", "confirm"),
    ("correct_prepaid_renewal_origin.py", "confirm"),
)
SHADOW_FROZEN_COMMANDS = (
    "capture-renewal-terms",
    "correct-renewal-terms",
    "execute-reviewed-prepaid-service-renewal",
    "correct-legacy-renewal-tax-invoice",
    "correct-unused-prepaid-renewal",
)


def _main_body(source: str) -> list[ast.stmt]:
    tree = ast.parse(source)
    main = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "main"
    )
    return main.body


def _guard_precedes_database_work(source: str, mode: str) -> bool:
    body = _main_body(source)
    for index, statement in enumerate(body):
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id == "refuse_apply"
        ):
            argument = statement.value.args[0]
            if mode == "apply":
                if not (
                    isinstance(argument, ast.Attribute)
                    and argument.attr == "apply"
                    and isinstance(argument.value, ast.Name)
                    and argument.value.id == "args"
                ):
                    return False
            elif mode == "apply_or_rollback":
                if not (
                    isinstance(argument, ast.BoolOp)
                    and isinstance(argument.op, ast.Or)
                    and len(argument.values) == 2
                    and all(
                        isinstance(value, ast.Attribute)
                        and isinstance(value.value, ast.Name)
                        and value.value.id == "args"
                        for value in argument.values
                    )
                    and argument.values[0].attr == "apply"
                    and argument.values[1].attr == "rollback"
                ):
                    return False
            elif mode == "optional_apply":
                if not (
                    isinstance(argument, ast.Call)
                    and isinstance(argument.func, ast.Name)
                    and argument.func.id == "bool"
                    and len(argument.args) == 1
                    and isinstance(argument.args[0], ast.Call)
                    and isinstance(argument.args[0].func, ast.Name)
                    and argument.args[0].func.id == "getattr"
                    and isinstance(argument.args[0].args[0], ast.Name)
                    and argument.args[0].args[0].id == "args"
                    and isinstance(argument.args[0].args[1], ast.Constant)
                    and argument.args[0].args[1].value == "apply"
                ):
                    return False
            elif mode.startswith("command:"):
                expected_command = mode.partition(":")[2]
                if not (
                    isinstance(argument, ast.Compare)
                    and isinstance(argument.left, ast.Attribute)
                    and isinstance(argument.left.value, ast.Name)
                    and argument.left.value.id == "args"
                    and argument.left.attr == "command"
                    and len(argument.ops) == 1
                    and isinstance(argument.ops[0], ast.Eq)
                    and len(argument.comparators) == 1
                    and isinstance(argument.comparators[0], ast.Constant)
                    and argument.comparators[0].value == expected_command
                ):
                    return False
            elif mode == "shadow_commands":
                if not (
                    isinstance(argument, ast.Compare)
                    and isinstance(argument.left, ast.Attribute)
                    and isinstance(argument.left.value, ast.Name)
                    and argument.left.value.id == "args"
                    and argument.left.attr == "command"
                    and len(argument.ops) == 1
                    and isinstance(argument.ops[0], ast.In)
                    and len(argument.comparators) == 1
                    and isinstance(argument.comparators[0], ast.Set)
                    and len(argument.comparators[0].elts) == len(SHADOW_FROZEN_COMMANDS)
                    and all(
                        isinstance(item, ast.Constant) and isinstance(item.value, str)
                        for item in argument.comparators[0].elts
                    )
                    and tuple(
                        item.value
                        for item in argument.comparators[0].elts
                        if isinstance(item, ast.Constant)
                    )
                    == SHADOW_FROZEN_COMMANDS
                ):
                    return False
            elif not (isinstance(argument, ast.Name) and argument.id == "execute"):
                return False
            if mode in {
                "apply",
                "apply_or_rollback",
                "optional_apply",
                "shadow_commands",
            } or mode.startswith("command:"):
                if index == 0:
                    return False
                parsed = body[index - 1]
                if not (
                    isinstance(parsed, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "args"
                        for target in parsed.targets
                    )
                    and isinstance(parsed.value, ast.Call)
                    and isinstance(parsed.value.func, ast.Attribute)
                    and parsed.value.func.attr == "parse_args"
                ):
                    return False
            elif mode == "execute":
                prefix = body[:index]
                if any(
                    not (
                        isinstance(statement, ast.Expr)
                        and isinstance(statement.value, ast.Constant)
                        and isinstance(statement.value.value, str)
                    )
                    for statement in prefix
                ):
                    return False
            earlier = ast.Module(body=body[:index], type_ignores=[])
            return not any(
                isinstance(node, ast.Call)
                and (
                    (
                        isinstance(node.func, ast.Name)
                        and node.func.id in {"SessionLocal", "create_engine"}
                    )
                    or (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr
                        in {"create_session", "owner_command_session", "begin"}
                    )
                )
                for node in ast.walk(earlier)
            )
    return False


@pytest.mark.parametrize("filename", APPLY_CLIS)
def test_apply_repair_clis_refuse_before_database_work(filename: str) -> None:
    source = (ROOT / "scripts" / "billing" / filename).read_text()
    mode = (
        "apply_or_rollback" if filename == "apply_billing_remediation.py" else "apply"
    )
    assert _guard_precedes_database_work(source, mode)


@pytest.mark.parametrize("filename", EXECUTE_CLIS)
def test_execute_repair_clis_refuse_before_database_or_backup_work(
    filename: str,
) -> None:
    source = (ROOT / "scripts" / "billing" / filename).read_text()
    body = _main_body(source)
    # These mains have a docstring followed directly by the execution guard.
    first = body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        body = body[1:]
    assert body and isinstance(body[0], ast.Expr)
    assert _guard_precedes_database_work(source, "execute")
    assert body[0].value.func.id == "refuse_apply"


@pytest.mark.parametrize("filename", ONE_OFF_APPLY_CLIS)
def test_one_off_billing_apply_clis_refuse_before_database_work(
    filename: str,
) -> None:
    source = (ROOT / "scripts" / "one_off" / filename).read_text()
    mode = (
        "optional_apply" if filename == "cutover_reconstructed_balance.py" else "apply"
    )
    assert _guard_precedes_database_work(source, mode)


@pytest.mark.parametrize(("filename", "mutating_command"), SUBCOMMAND_WRITE_CLIS)
def test_billing_subcommand_write_modes_are_guarded_immediately(
    filename: str, mutating_command: str
) -> None:
    source = (ROOT / "scripts" / "billing" / filename).read_text()
    assert _guard_precedes_database_work(source, f"command:{mutating_command}")


def test_billing_target_shadow_freezes_exact_historical_repair_modes() -> None:
    source = (ROOT / "scripts" / "billing" / "billing_target_shadow.py").read_text()
    assert _guard_precedes_database_work(source, "shadow_commands")


def _compile_shadow_main(
    command: str,
    *,
    database_calls: list[None],
    handler_calls: list[str],
    evidence_calls: list[str],
) -> object:
    class ParserStub:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def add_argument(self, *_args: object, **_kwargs: object) -> None:
            pass

        def add_subparsers(self, **_kwargs: object) -> SubparsersStub:
            return SubparsersStub()

        def set_defaults(self, **_kwargs: object) -> None:
            pass

        def parse_args(self) -> SimpleNamespace:
            return SimpleNamespace(
                command=command,
                fact_file="captured.json",
                func=dispatch,
            )

    class SubparsersStub:
        def add_parser(self, *_args: object, **_kwargs: object) -> ParserStub:
            return ParserStub()

    def dispatch(_db: object, args: SimpleNamespace) -> int:
        handler_calls.append(args.command)
        return 17

    def database_trap() -> None:
        database_calls.append(None)
        raise _DatabaseReached

    def evidence_route(args: SimpleNamespace) -> int:
        evidence_calls.append(args.command)
        return 23

    source_path = ROOT / "scripts" / "billing" / "billing_target_shadow.py"
    tree = ast.parse(source_path.read_text(), filename=str(source_path))
    main_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    globals_for_main: dict[str, object] = {
        "__doc__": "hosted AST canary",
        "argparse": SimpleNamespace(
            ArgumentParser=ParserStub,
            RawDescriptionHelpFormatter=argparse.RawDescriptionHelpFormatter,
        ),
        "refuse_apply": refuse_apply,
        "SessionLocal": database_trap,
        "_cmd_preview_module_accounting_export": evidence_route,
    }
    for node in ast.walk(main_node):
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id not in globals_for_main
        ):
            globals_for_main[node.id] = dispatch
    module = ast.fix_missing_locations(ast.Module(body=[main_node], type_ignores=[]))
    exec(compile(module, str(source_path), "exec"), globals_for_main)  # noqa: S102
    return globals_for_main["main"]


@pytest.mark.parametrize("command", SHADOW_FROZEN_COMMANDS)
def test_billing_target_shadow_frozen_modes_stop_before_session_and_handler(
    command: str,
) -> None:
    database_calls: list[None] = []
    handler_calls: list[str] = []
    main = _compile_shadow_main(
        command,
        database_calls=database_calls,
        handler_calls=handler_calls,
        evidence_calls=[],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert database_calls == []
    assert handler_calls == []


@pytest.mark.parametrize(
    ("command", "uses_database"),
    (
        ("preview-renewal-terms", True),
        ("fire-due-timers", True),
        ("request-renewal-term-record", True),
        ("approve-renewal-term-record", True),
        ("activate-subledger-authority", True),
        ("preview-module-accounting-export", False),
    ),
)
def test_billing_target_shadow_nonfrozen_routes_remain_reachable(
    command: str, uses_database: bool
) -> None:
    database_calls: list[None] = []
    handler_calls: list[str] = []
    evidence_calls: list[str] = []
    main = _compile_shadow_main(
        command,
        database_calls=database_calls,
        handler_calls=handler_calls,
        evidence_calls=evidence_calls,
    )
    if uses_database:
        with pytest.raises(_DatabaseReached):
            main()
        assert database_calls == [None]
        assert handler_calls == []
        assert evidence_calls == []
    else:
        assert main() == 23
        assert database_calls == []
        assert handler_calls == []
        assert evidence_calls == [command]


@pytest.mark.parametrize(
    ("directory", "filename"),
    (
        ("billing", APPLY_CLIS[0]),
        ("one_off", "soft_delete_failed_payment_reversal_pairs.py"),
        ("one_off", "repair_billing_date_anchors.py"),
        ("one_off", "repair_stale_prepaid_billing_anchors.py"),
        ("one_off", "align_catalog_vat_flags.py"),
    ),
)
def test_guard_detector_rejects_removed_or_late_guard(
    directory: str, filename: str
) -> None:
    source = (ROOT / "scripts" / directory / filename).read_text()
    module = ast.parse(source)
    main = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    without_module = copy.deepcopy(module)
    without_main = next(
        node
        for node in without_module.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    without_main.body = [
        statement
        for statement in without_main.body
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id == "refuse_apply"
        )
    ]
    assert not _guard_precedes_database_work(ast.unparse(without_module), "apply")

    moved_module = copy.deepcopy(module)
    moved_main = next(
        node
        for node in moved_module.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    guard = next(
        statement
        for statement in moved_main.body
        if isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id == "refuse_apply"
    )
    moved_main.body.remove(guard)
    moved_main.body.append(guard)
    assert not _guard_precedes_database_work(ast.unparse(moved_module), "apply")


def test_guard_detector_rejects_late_subcommand_guard() -> None:
    source = (
        ROOT / "scripts" / "billing" / "repair_prepaid_paid_invoice_period.py"
    ).read_text()
    module = ast.parse(source)
    main = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    guard = next(
        statement
        for statement in main.body
        if isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id == "refuse_apply"
    )
    main.body.remove(guard)
    main.body.append(guard)
    assert not _guard_precedes_database_work(ast.unparse(module), "command:approve")


def test_write_mode_refuses_and_preview_mode_passes_guard(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        refuse_apply(True)
    assert exc.value.code == 1
    assert capsys.readouterr().err.strip() == REFUSAL_MESSAGE

    refuse_apply(False)
    assert capsys.readouterr().err == ""


class _DatabaseReached(Exception):
    pass


def _compile_main(
    filename: str, directory: str, namespace: dict[str, object]
) -> object:
    source_path = ROOT / "scripts" / directory / filename
    tree = ast.parse(source_path.read_text(), filename=str(source_path))
    main = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    module = ast.fix_missing_locations(ast.Module(body=[main], type_ignores=[]))
    exec(compile(module, str(source_path), "exec"), namespace)  # noqa: S102
    return namespace["main"]


@pytest.mark.parametrize(
    "filename",
    (
        "reconcile_prepaid_drawdown_reversals.py",
        "soft_delete_failed_payment_reversal_pairs.py",
        "repair_billing_date_anchors.py",
        "repair_stale_prepaid_billing_anchors.py",
        "align_catalog_vat_flags.py",
    ),
)
def test_one_off_actual_main_apply_flag_refuses_before_session(
    filename: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    session_calls: list[None] = []

    def session_trap() -> None:
        session_calls.append(None)
        raise _DatabaseReached

    namespace: dict[str, object] = {
        "__doc__": "hosted AST canary",
        "argparse": argparse,
        "SessionLocal": session_trap,
        "refuse_apply": refuse_apply,
        "Path": Path,
        "DEFAULT_OUTPUT": "unused-preview.csv",
        "DEFAULT_LIMIT": 500,
        "UUID": UUID,
    }
    main = _compile_main(filename, "one_off", namespace)
    monkeypatch.setattr(sys, "argv", [filename])
    with pytest.raises(_DatabaseReached):
        main()
    assert session_calls == [None]
    assert capsys.readouterr().err == ""

    session_calls.clear()
    monkeypatch.setattr(
        sys,
        "argv",
        [filename, "--apply"],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert session_calls == []
    assert capsys.readouterr().err.strip() == REFUSAL_MESSAGE


def test_billing_remediation_rollback_refuses_before_session(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    session_calls: list[None] = []

    def session_trap() -> None:
        session_calls.append(None)
        raise _DatabaseReached

    namespace: dict[str, object] = {
        "__doc__": "hosted AST canary",
        "argparse": argparse,
        "SessionLocal": session_trap,
        "refuse_apply": refuse_apply,
        "sys": sys,
    }
    main = _compile_main("apply_billing_remediation.py", "billing", namespace)
    monkeypatch.setattr(
        sys,
        "argv",
        ["apply_billing_remediation", "--rollback", "reviewed-manifest.json"],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert session_calls == []
    assert "disabled by policy" in capsys.readouterr().err


def test_programmatic_execute_true_refuses_before_session(
    capsys,
) -> None:
    session_calls: list[None] = []

    def session_trap() -> None:
        session_calls.append(None)
        raise _DatabaseReached

    namespace: dict[str, object] = {
        "__doc__": "hosted AST canary",
        "SessionLocal": session_trap,
        "refuse_apply": refuse_apply,
    }
    main = _compile_main("purge_phantom_notifications.py", "billing", namespace)
    with pytest.raises(_DatabaseReached):
        main(False)
    assert session_calls == [None]

    session_calls.clear()
    with pytest.raises(SystemExit) as exc:
        main(True)
    assert exc.value.code == 1
    assert session_calls == []
    assert "disabled by policy" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("filename", "command", "should_refuse"),
    (
        ("repair_prepaid_paid_invoice_period.py", "approve", True),
        ("repair_prepaid_paid_invoice_period.py", "request", False),
        ("repair_prepaid_paid_invoice_period.py", "list", False),
        ("repair_prepaid_paid_invoice_period.py", "preview", False),
        ("return_legacy_over_allocation.py", "confirm", True),
        ("return_legacy_over_allocation.py", "preview", False),
        ("correct_prepaid_renewal_origin.py", "confirm", True),
        ("correct_prepaid_renewal_origin.py", "preview", False),
    ),
)
def test_financial_subcommands_refuse_and_nonfinancial_modes_remain_routable(
    filename: str,
    command: str,
    should_refuse: bool,
    capsys,
) -> None:
    handler_calls: list[str] = []

    class ParserStub:
        def parse_args(self, argv: list[str] | None = None) -> SimpleNamespace:
            assert argv == [command]

            def handler(_args: object) -> int:
                handler_calls.append(command)
                return 29

            return SimpleNamespace(command=command, func=handler)

    namespace: dict[str, object] = {
        "build_parser": ParserStub,
        "refuse_apply": refuse_apply,
        "DomainError": Exception,
        "_EXIT_REFUSED": 3,
        "_emit": lambda _payload: None,
    }
    main = _compile_main(filename, "billing", namespace)
    if should_refuse:
        with pytest.raises(SystemExit) as exc:
            main([command])
        assert exc.value.code == 1
        assert handler_calls == []
        assert "disabled by policy" in capsys.readouterr().err
    else:
        assert main([command]) == 29
        assert handler_calls == [command]
        assert capsys.readouterr().err == ""
