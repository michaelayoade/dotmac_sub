"""Guardrails: the customer portal never receives admin/operational data.

The portal branding context previously injected unscoped system-wide admin
counts (get_sidebar_stats) into every portal page — including the
unauthenticated login screen — the same defect class as the public landing
page's subscriber leak. These tests keep both closed.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

_FORBIDDEN_IN_PORTAL = (
    "app.services.web_admin",  # admin operational aggregates
)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_customer_portal_modules_never_import_admin_services():
    portal_dir = _REPO / "app" / "web" / "customer"
    offenders: list[str] = []
    for path in sorted(portal_dir.glob("*.py")):
        for name in _imports(path):
            if any(
                name == bad or name.startswith(bad + ".")
                for bad in _FORBIDDEN_IN_PORTAL
            ):
                offenders.append(f"{path.name}: {name}")
    assert not offenders, (
        "customer portal modules import admin services (unscoped operational "
        f"data must not reach the portal context): {offenders}"
    )


def test_branding_context_carries_no_admin_counts():
    source = (_REPO / "app" / "web" / "customer" / "branding.py").read_text(
        encoding="utf-8"
    )
    for marker in ("get_sidebar_stats", "service_orders", "notifications_unread"):
        assert marker not in source, (
            f"admin operational marker {marker!r} reappeared in the portal "
            "branding context"
        )
