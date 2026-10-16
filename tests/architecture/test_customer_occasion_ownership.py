"""Guards against parallel annual greeting writers and direct delivery."""

import ast
from pathlib import Path

from app.services.sot_manifest import contract_validation_errors
from app.services.sot_registry.customer_occasion_contracts import (
    CUSTOMER_OCCASION_SERVICE,
)
from app.services.sot_registry.registry import all_services

ROOT = Path(__file__).resolve().parents[2]


def test_occasion_owner_has_complete_registered_contract() -> None:
    assert CUSTOMER_OCCASION_SERVICE in all_services()
    assert not contract_validation_errors(
        CUSTOMER_OCCASION_SERVICE, service_names={item.name for item in all_services()}
    )


def test_occasion_adapters_do_not_decide_or_write() -> None:
    for relative in (
        "app/tasks/customer_occasions.py",
        "app/web/admin/customer_occasions.py",
    ):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        forbidden = {
            "commit",
            "rollback",
            "add",
            "delete",
            "execute",
            "query",
            "flush",
            "begin_nested",
        }
        assert not [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in forbidden
        ]


def test_occasion_owner_only_queues_through_intents() -> None:
    source = (ROOT / "app/services/customer_occasions.py").read_text(encoding="utf-8")
    assert "execute_owner_command" in source and "CommunicationIntent(" in source
    for forbidden in (
        "send_email(",
        "smtplib",
        "db.commit(",
        "db.rollback(",
        "created_at.date()",
    ):
        assert forbidden not in source
