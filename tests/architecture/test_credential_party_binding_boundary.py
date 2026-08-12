"""One runtime writer owns every field in the credential Party projection."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = "app/services/credential_party_binding.py"
FIELDS = {
    "party_id",
    "authentication_binding_id",
    "tenant_id",
    "party_bound_at",
    "party_binding_source",
    "party_binding_reason",
}


def _credential_projection_writers(app_root: Path) -> dict[str, set[str]]:
    writers: dict[str, set[str]] = {}
    for path in app_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign | ast.AugAssign):
                targets = [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "credential"
                    and target.attr in FIELDS
                ):
                    relative = str(path.relative_to(app_root.parent))
                    writers.setdefault(relative, set()).add(target.attr)
    return writers


def test_credential_projection_has_one_complete_runtime_writer() -> None:
    writers = _credential_projection_writers(PROJECT_ROOT / "app")

    assert writers == {OWNER: FIELDS}


def test_writer_detector_sensitivity(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    violation = app / "bad_adapter.py"
    violation.write_text("credential.party_id = invented_party_id\n", encoding="utf-8")

    writers = _credential_projection_writers(app)

    assert writers == {"app/bad_adapter.py": {"party_id"}}
