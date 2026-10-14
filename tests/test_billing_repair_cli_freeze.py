"""Static canaries for the billing repair CLI write-mode freeze."""

from __future__ import annotations

import argparse
import ast
import copy
import sys
from pathlib import Path
from types import SimpleNamespace

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
            elif not (isinstance(argument, ast.Name) and argument.id == "execute"):
                return False
            if mode in {
                "apply",
                "apply_or_rollback",
                "optional_apply",
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


def test_guard_detector_rejects_removed_or_late_guard() -> None:
    source = (ROOT / "scripts" / "billing" / APPLY_CLIS[0]).read_text()
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


def test_one_off_actual_main_apply_flag_refuses_before_session(
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
    }
    main = _compile_main(
        "reconcile_prepaid_drawdown_reversals.py", "one_off", namespace
    )
    monkeypatch.setattr(sys, "argv", ["reconcile_prepaid_drawdown_reversals"])
    with pytest.raises(_DatabaseReached):
        main()
    assert session_calls == [None]

    session_calls.clear()
    monkeypatch.setattr(
        sys,
        "argv",
        ["reconcile_prepaid_drawdown_reversals", "--apply"],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert session_calls == []
    assert "disabled by policy" in capsys.readouterr().err


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
