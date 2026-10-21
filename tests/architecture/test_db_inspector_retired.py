"""The admin DB inspector is retired (cleanup M7).

It accepted free-form SQL from the browser. Nothing may reintroduce an
ad-hoc SQL console in the admin application.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

RETIRED_FILES = (
    "app/services/web_system_db_inspector.py",
    "templates/admin/system/db_inspector.html",
)
RETIRED_TOKENS = (
    "web_system_db_inspector",
    "db_inspector",
    "db-inspector",
    "system:db_admin",
    "dbi_confirm",
)


def test_db_inspector_files_are_gone() -> None:
    for relative in RETIRED_FILES:
        assert not (ROOT / relative).exists(), relative


def test_nothing_references_the_db_inspector() -> None:
    offenders: list[str] = []
    for base in ("app", "templates", "scripts", "static"):
        for path in (ROOT / base).rglob("*"):
            if not path.is_file() or path.suffix not in {
                ".py",
                ".html",
                ".js",
                ".json",
            }:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(token in text for token in RETIRED_TOKENS):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_db_inspector_routes_are_not_mounted() -> None:
    from app.web.admin import router

    paths = {getattr(route, "path", "") for route in router.routes}
    assert not any("db-inspector" in path for path in paths)
