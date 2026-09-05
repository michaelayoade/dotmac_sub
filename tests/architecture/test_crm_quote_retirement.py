"""Retired transport cannot return through a scheduler, task or lazy read."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_quote_transport_methods_are_removed_from_all_layers():
    retired = {"get_portal_quotes", "request_portal_quote", "accept_portal_quote"}
    for path in (
        "app/services/crm_client.py",
        "app/services/integrations/crm_capability.py",
        "app/services/integrations/connectors/dotmac_crm.py",
    ):
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        assert not retired.intersection(
            n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
        )


def test_queued_quote_tasks_cannot_open_sessions_or_call_crm():
    source = (ROOT / "app/tasks/quotes.py").read_text(encoding="utf-8")
    for forbidden in (
        "create_session",
        "capability_client",
        "reconcile_all",
        "reconcile_subscriber",
    ):
        assert forbidden not in source


def test_quote_schedules_are_retired_by_task_identity():
    source = (ROOT / "app/services/scheduler_config.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    retired = {
        "app.tasks.quotes.reconcile_quote_mirror",
        "app.tasks.quotes.refresh_quote_mirror_for_subscriber",
    }
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            values = {
                a.value
                for a in node.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            }
            if node.func.id == "_retire_scheduled_task":
                found.update(values & retired)
            elif node.func.id == "_sync_scheduled_task":
                assert not any(
                    isinstance(k.value, ast.Constant) and k.value.value in retired
                    for k in node.keywords
                )
    assert found == retired


def test_bulk_transport_still_uses_erp_contract():
    source = (ROOT / "app/services/dotmac_erp/client.py").read_text(encoding="utf-8")
    assert '"/api/v1/sync/sub/bulk"' in source
    assert '"X-API-Key"' in source
    assert "body=%s" not in source
