"""New domain/application owners stay independent of HTTP frameworks."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PURE_SERVICE_MODULES = (
    ROOT / "app/services/dotmac_erp/purchase_invoice_sync.py",
    ROOT / "app/services/vendor_payment_status.py",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    return imports


def test_vendor_payment_owner_and_reconciler_have_no_http_dependency() -> None:
    for path in PURE_SERVICE_MODULES:
        imports = _imports(path)
        coupled = sorted(
            module
            for module in imports
            if module == "fastapi"
            or module.startswith("fastapi.")
            or module == "starlette"
            or module.startswith("starlette.")
        )
        assert not coupled, f"{path.relative_to(ROOT)} imports HTTP types: {coupled}"
