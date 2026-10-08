"""Receipt admission owns persistence; adapters never report broker failure as rejection."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_bulk_message_adapter_uses_receipt_identity_and_status() -> None:
    source = (ROOT / "app/web/admin/customers.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    route = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "bulk_send_customer_message"
    )
    body = ast.get_source_segment(source, route)
    assert "customer_bulk_messages.accept" in body
    assert body.index("customer_bulk_messages.accept") < body.index("enqueue_task")
    assert "prepared.payload_json" not in body
    assert "status_code=503" not in body
    assert "request_id=request_id" in body


def test_materialization_participant_cannot_commit_or_run_without_owner() -> None:
    source = (ROOT / "app/services/web_customer_actions.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    core = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_evaluate_bulk_message"
    )
    for node in ast.walk(core):
        assert not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"commit", "rollback", "begin_nested"}
        )
    evaluation = (ROOT / "app/services/customer_bulk_message_evaluation.py").read_text(
        encoding="utf-8"
    )
    assert "owner_command_active" in evaluation
    assert "_evaluate_bulk_message_domain(db=db, spec=spec)" in evaluation
    assert "class PreparedBulkMessageDispatch" not in source


def test_receipt_drain_is_permanent_and_both_screens_recover_status() -> None:
    scheduler = (ROOT / "app/services/scheduler.py").read_text(encoding="utf-8")
    assert '"app.tasks.notifications.dispatch_customer_bulk_messages"' in scheduler
    for page in ("index.html", "detail.html"):
        source = (ROOT / "templates/admin/customers" / page).read_text(encoding="utf-8")
        assert 'include "admin/customers/_bulk_send_status.html"' in source
        assert "DotmacCustomerBulkSend.send" in source
        assert "Failed to queue the message. Please try again." not in source
