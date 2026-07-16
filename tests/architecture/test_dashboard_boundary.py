"""Guardrail: the dashboard web service composes owners, it does not derive.

Phase 5 of docs/DASHBOARD_OVERVIEW_PAGE_CONTRACT.md. Every number on the
overview comes from a domain read owner; this service may resolve settings and
assemble context, but it may not aggregate domain tables or run raw SQL. This
test keeps the migration from regressing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "services"
    / "web_admin_dashboard.py"
)

# Config/identity imports the dashboard may keep: settings resolution and the
# audit actor vocabulary. Domain tables must be read via their owners.
_ALLOWED_MODEL_IMPORTS = {
    "app.models.domain_settings",
    "app.models.audit",
    "app.models.subscriber",  # legacy actor-lookup typing only; no queries
}


def test_dashboard_service_runs_no_raw_sql_or_aggregates():
    source = _SOURCE.read_text(encoding="utf-8")
    assert "sa_text" not in source, "raw SQL reappeared in the dashboard service"
    assert not re.search(r"\btext\(", source), "raw SQL reappeared"
    assert not re.search(r"func\.(count|sum|avg|max|min)\(", source), (
        "the dashboard service aggregates domain tables again — move the "
        "aggregation to the owning read service"
    )


def test_dashboard_service_queries_only_settings():
    """db.query(...) may target settings only; domain reads go through owners."""
    source = _SOURCE.read_text(encoding="utf-8")
    queried = re.findall(r"db\.query\(\s*([A-Za-z_]+)", source)
    offenders = [name for name in queried if name != "DomainSetting"]
    assert not offenders, (
        f"dashboard service queries domain models directly: {offenders}"
    )


def test_dashboard_service_model_imports_are_config_only():
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if (
                node.module.startswith("app.models")
                and node.module not in _ALLOWED_MODEL_IMPORTS
            ):
                offenders.append(node.module)
    assert not offenders, (
        f"dashboard service imports domain models: {offenders}; read them "
        "through their owning services instead"
    )
