"""Completion transports and projection cannot acquire business authority."""

import ast
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "path",
    [
        "app/api/field_completion_review.py",
        "app/web/admin/field_completion_review.py",
        "app/services/field_completion_review_projection.py",
    ],
)
def test_completion_adapters_have_no_parallel_writer(path: str) -> None:
    tree = ast.parse(Path(path).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("app.models")
        if isinstance(node, ast.Call):
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            assert name not in {
                "execute_owner_command",
                "commit",
                "flush",
                "add",
                "delete",
                "begin_nested",
            }


def test_field_router_has_no_staff_permission_floor() -> None:
    from app.api.field_completion_review import field_router, router

    assert not field_router.dependencies
    assert not router.dependencies


def test_completion_projection_has_complete_read_contract() -> None:
    from app.services.sot_manifest import TransactionMode
    from app.services.sot_registry.domains.provisioning_operations.field_completion_projection import (
        SERVICES,
    )

    service = SERVICES[0]
    assert service.name == "ui.field_completion_review_projection"
    assert service.contract is not None
    assert service.contract.transaction.mode == TransactionMode.READ_ONLY
    assert "operations.field_completion" in service.depends_on
