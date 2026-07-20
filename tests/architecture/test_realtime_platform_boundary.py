from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_domain_services_do_not_import_websocket_transport() -> None:
    offenders: list[str] = []
    for path in (PROJECT_ROOT / "app/services").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports_transport = any(
            (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").startswith("app.websocket")
            )
            or (
                isinstance(node, ast.Import)
                and any(alias.name.startswith("app.websocket") for alias in node.names)
            )
            for node in ast.walk(tree)
        )
        if imports_transport:
            offenders.append(path.relative_to(PROJECT_ROOT).as_posix())
    assert offenders == []


def test_realtime_owner_is_transport_neutral() -> None:
    source = (PROJECT_ROOT / "app/services/realtime_platform.py").read_text(
        encoding="utf-8"
    )
    assert "fastapi" not in source.lower()
    assert "starlette" not in source.lower()
    assert "HTTPException" not in source


def test_legacy_broker_prefix_has_no_application_publishers() -> None:
    offenders: list[str] = []
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        if "inbox_ws:" in path.read_text(encoding="utf-8"):
            offenders.append(path.relative_to(PROJECT_ROOT).as_posix())
    assert offenders == []
