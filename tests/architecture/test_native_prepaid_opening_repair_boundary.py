"""Guard the narrow Sub-native prepaid-opening repair boundary."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_native_repair_has_one_owner_transaction_and_no_parallel_writer() -> None:
    owner = _read("app/services/billing/subledger_opening.py")
    operation = owner[owner.index("def repair_native_prepaid_opening(") :]
    operation = operation[: operation.index("def capture_customer_subledger")]

    assert operation.count("execute_owner_command(") == 1
    assert "stage_posting_group(" in operation
    assert "db.commit(" not in operation
    assert "db.rollback(" not in operation
    assert "begin_nested(" not in operation
    assert "text(" not in operation
    assert "apply_prepaid_funding_reconstruction" not in operation


def test_native_repair_keeps_complete_cohort_reconstruction_unchanged() -> None:
    reconstruction = _read("app/services/prepaid_funding_reconstruction.py")
    repair = _read("app/services/billing/subledger_opening.py")

    assert "reconstruction_source_cohort_incomplete" in reconstruction
    assert "expected_candidate_hash = candidate_cohort_sha256" in reconstruction
    assert "NativePrepaidOpeningRepair" not in reconstruction
    assert "manifest_sha256=" not in repair
    assert "candidate_cohort_sha256=" not in repair


def test_native_repair_apply_is_locked_and_database_arbitrated() -> None:
    owner = _read("app/services/billing/subledger_opening.py")
    model = _read("app/models/customer_subledger.py")
    migration = _read("alembic/versions/624_native_prepaid_opening_repairs.py")

    for locked_type in (
        "Subscriber",
        "SystemUser",
        "PrepaidFundingReconstructionBatch",
        "CustomerSubledgerAuthorityCutover",
        "PrepaidFundingBaseline",
        "CustomerSubledgerOpeningPosition",
        "SplynxBillingTransaction",
    ):
        assert locked_type in owner
    assert ".with_for_update()" in owner
    assert "uq_native_prepaid_opening_account_currency" in model
    assert "uq_native_prepaid_opening_idempotency" in model
    assert "uq_customer_subledger_opening_native_repair" in migration
    assert "native_prepaid_opening_repairs_append_only" in migration


def test_native_repair_cli_is_dry_run_first_and_apply_is_fingerprint_bound() -> None:
    cli = _read("scripts/billing/repair_native_prepaid_opening.py")

    assert 'parser.add_argument("--apply", action="store_true")' in cli
    assert "if not args.apply:" in cli
    assert "args.fingerprint" in cli
    assert "args.operator_system_user_id" in cli
    assert "args.reason" in cli
    assert "args.idempotency_key" in cli
    assert "expected_preview_fingerprint=args.fingerprint" in cli


def test_native_repair_does_not_mutate_forbidden_business_state() -> None:
    owner = _read("app/services/billing/subledger_opening.py")
    operation = owner[owner.index("def _repair_native_prepaid_opening(") :]
    operation = operation[: operation.index("def capture_customer_subledger")]

    for forbidden in (
        "LedgerEntry(",
        "Payment(",
        "Invoice(",
        "subscription.status",
        "access_state",
        "next_billing_at",
    ):
        assert forbidden not in operation
