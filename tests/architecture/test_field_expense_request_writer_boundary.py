"""Architecture guard for the operations.expense_requests write boundary."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OWNER = Path("app/services/field/expense_requests.py")
PROJECTION_WRITER = Path("app/services/dotmac_erp/expense_sync.py")
EXPENSE_MUTABLE_FIELDS = {
    "status",
    "approved_at",
    "rejected_at",
    "paid_at",
    "rejection_reason",
    "expense_system",
    "expense_claim_reference",
    "expense_claim_number",
    "expense_claim_status",
    "metadata_",
}


def _production_python_paths():
    for root_name in ("app", "scripts"):
        yield from (ROOT / root_name).rglob("*.py")


def test_every_field_expense_request_constructor_is_in_the_registered_owner() -> None:
    writers: list[str] = []
    for path in _production_python_paths():
        relative = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if name == "FieldExpenseRequest" and relative != OWNER:
                writers.append(f"{relative}:{node.lineno}")
    assert writers == [], (
        "FieldExpenseRequest creation must go through operations.expense_requests: "
        + ", ".join(writers)
    )


def test_legacy_generic_expense_writers_remain_closed() -> None:
    source = (ROOT / OWNER).read_text(encoding="utf-8")
    adapter = (ROOT / "app/api/field/expense_requests.py").read_text(encoding="utf-8")
    assert "def create(" not in source
    assert "def submit(" not in source
    assert "HTTP_410_GONE" in adapter
    assert "def enqueue_expense_claim(" not in (
        ROOT / "app/services/dotmac_erp/expense_sync.py"
    ).read_text(encoding="utf-8")
    assert "_is_retired_preapproval_expense_event" in (
        ROOT / "app/services/dotmac_erp/outbox.py"
    ).read_text(encoding="utf-8")


def test_field_expense_request_mutations_have_enumerated_owners() -> None:
    writers: set[Path] = set()
    for path in _production_python_paths():
        source = path.read_text(encoding="utf-8")
        if "FieldExpenseRequest" not in source:
            continue
        relative = path.relative_to(ROOT)
        tree = ast.parse(source, filename=str(relative))
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = (
                    list(node.targets)
                    if isinstance(node, ast.Assign)
                    else [node.target]
                )
            if any(
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "request"
                and target.attr in EXPENSE_MUTABLE_FIELDS
                for target in targets
            ):
                writers.add(relative)
    assert writers == {OWNER, PROJECTION_WRITER}


def test_deployed_chain_already_owns_the_non_null_work_order_fk() -> None:
    model = (ROOT / "app/models/field_expense.py").read_text(encoding="utf-8")
    migration = (
        ROOT / "alembic/versions/223_work_order_dispatch_foundation.py"
    ).read_text(encoding="utf-8")
    assert "work_order_mirror_id: Mapped[uuid.UUID]" in model
    assert 'ForeignKey("work_order.id", ondelete="CASCADE")' in model
    assert (
        '"work_order_mirror_id", postgresql.UUID(as_uuid=True), nullable=False'
        in migration
    )
