from __future__ import annotations

import ast
from pathlib import Path


def _registered_router_modules(repository_root: Path) -> set[str]:
    tree = ast.parse((repository_root / "app" / "main.py").read_text())
    router_spec_names = {"_CORE_ROUTER_SPECS", "_DEFERRED_API_ROUTER_SPECS"}
    modules: set[str] = set()

    for node in tree.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id not in router_spec_names or node.value is None:
            continue
        router_specs = ast.literal_eval(node.value)
        modules.update(module for module, *_rest in router_specs)

    return modules


def test_unscoped_legacy_web_domain_router_is_retired() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    modules = _registered_router_modules(repository_root)

    assert "app.web_domains" not in modules

    assert not (repository_root / "app" / "web_domains.py").exists()
    assert not (repository_root / "templates" / "domain.html").exists()


def test_shared_login_exposes_vendor_entry_with_scoped_return_path() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    login_template = (repository_root / "templates" / "auth" / "login.html").read_text()

    assert 'href="/auth/login?next=/vendor"' in login_template
