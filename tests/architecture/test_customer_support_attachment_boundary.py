"""Customer binary transport remains a scoped typed support-owner adapter."""

import ast
from pathlib import Path


def test_customer_attachment_adapter_uses_owner_without_orm_or_storage_authority() -> (
    None
):
    source = Path("app/api/customer_support_attachments.py").read_text()
    tree = ast.parse(source)
    imports = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not any(name and name.startswith("app.models") for name in imports)
    calls = {
        ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert "web_support_tickets.customer_ticket_attachment_content" in calls
    assert "web_support_tickets.CustomerTicketAttachmentQuery" in calls
    assert not any(
        call.endswith((".commit", ".rollback", ".stream_file")) for call in calls
    )
    assert "storage_key" not in source and "public_url" not in source


def test_customer_attachment_router_mounted_only_under_me() -> None:
    tree = ast.parse(Path("app/api/me.py").read_text())
    mounts = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) == "router.include_router"
        and node.args
        and ast.unparse(node.args[0]) == "customer_support_attachments_router"
    ]
    assert len(mounts) == 1
