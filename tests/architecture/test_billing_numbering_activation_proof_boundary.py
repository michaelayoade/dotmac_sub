"""The delegated V2 owner and CLI are incapable of numbering writes."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = (
    "app/services/billing_numbering_activation_proof.py",
    "scripts/migration/billing_numbering_activation_proof.py",
)
SESSION_WRITES = {
    "add",
    "add_all",
    "flush",
    "commit",
    "rollback",
    "delete",
    "begin_nested",
}
FORBIDDEN = {
    "allocate",
    "configure_series",
    "advance_to_at_least",
    "execute_once",
    "generate_number_with_config",
    "generate_required_number",
    "with_for_update",
}


def forbidden_calls(source: str) -> set[str]:
    """Track session arguments/aliases without treating collections as sessions."""
    tree = ast.parse(source)
    sessions = {"db", "session", "_session"}
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            annotation = ast.unparse(node.annotation).strip("\"'")
            if annotation in ("Session", "sqlalchemy.orm.Session"):
                sessions.add(node.arg)

    def session_receiver(node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id in sessions
        if isinstance(node, ast.Attribute):
            return node.attr in sessions or session_receiver(node.value)
        return False

    # Preserve aliases even when a later traversal encounters their declaration.
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and session_receiver(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in sessions:
                        sessions.add(target.id)
                        changed = True
    violations: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else node.func.id
            if isinstance(node.func, ast.Name)
            else ""
        )
        if name in FORBIDDEN or (
            name in SESSION_WRITES
            and isinstance(node.func, ast.Attribute)
            and session_receiver(node.func.value)
        ):
            violations.add(name)
    return violations


def test_session_write_guard_distinguishes_collection_updates() -> None:
    assert forbidden_calls("db.add(record)") == {"add"}
    assert forbidden_calls("session.add_all(records)") == {"add_all"}
    assert forbidden_calls("writer = db\nwriter.flush()") == {"flush"}
    assert forbidden_calls("self.db.commit()") == {"commit"}
    assert forbidden_calls("def query(store: Session):\n    store.rollback()") == {
        "rollback"
    }
    assert not forbidden_calls("seen = set()\nseen.add(raw)")


def test_read_only_candidate_boundary() -> None:
    for path in FILES:
        source = (ROOT / path).read_text()
        assert not forbidden_calls(source), (path, forbidden_calls(source))
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("dotmac_numbering."), path
                assert not (node.module or "").startswith("dotmac_kernel.") or (
                    path.startswith("scripts/")
                    and node.module == "dotmac_kernel.settings_cache"
                ), path
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert (
                    not node.value.upper()
                    .lstrip()
                    .startswith(("UPDATE ", "INSERT ", "DELETE ", "ALTER ", "CREATE "))
                ), path
    owner = (ROOT / FILES[0]).read_text()
    assert 'OWNER = "financial.billing_numbering_readiness"' in owner
    assert "billing_numbering_readiness_report" in owner
    adapter = (ROOT / FILES[1]).read_text()
    assert "read_only_snapshot_session()" in adapter
    assert "billing_numbering_activation_proof_report(db, query=query)" in adapter
