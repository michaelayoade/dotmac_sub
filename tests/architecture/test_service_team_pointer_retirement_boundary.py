from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_pointer_retirement_cannot_import_crm_or_create_identity_topology():
    path = ROOT / "app/services/service_team_pointer_retirement.py"
    source = path.read_text()
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }

    assert not any(name.startswith("app.services.crm") for name in imports)
    assert "crm_client" not in source
    assert "Party(" not in source
    assert "SystemUser(" not in source
    assert "ServiceTeamMember(" not in source
    assert ".manager_person_id = None" in source
