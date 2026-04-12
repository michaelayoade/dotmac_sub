"""Architecture checks for thin web/api wrappers."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROUTE_DIRS = [PROJECT_ROOT / "app" / "web", PROJECT_ROOT / "app" / "api"]
DISALLOWED_PATTERNS = [
    re.compile(r"\bdb\.query\("),
    re.compile(r"\bdb\.execute\("),
    re.compile(r"\bselect\("),
]

# Files that legitimately need direct DB access (health checks, helpers)
EXCLUDED_FILES = {
    "health.py",  # Health checks require direct DB access
}


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for route_dir in ROUTE_DIRS:
        files.extend(
            path
            for path in route_dir.rglob("*.py")
            if path.is_file() and path.name not in EXCLUDED_FILES
        )
    return sorted(files)


def test_web_and_api_wrappers_do_not_issue_direct_queries() -> None:
    violations: list[str] = []

    for path in _iter_python_files():
        text = path.read_text(encoding="utf-8")
        for pattern in DISALLOWED_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                rel = path.relative_to(PROJECT_ROOT)
                violations.append(f"{rel}:{line} -> {pattern.pattern}")

    assert not violations, "\n".join(violations)


def test_admin_network_routes_are_not_registered_twice() -> None:
    """Catch legacy/split router overlap for OLT/ONT admin paths."""
    from app.web.admin import router

    seen: dict[tuple[tuple[str, ...], str], list[str]] = defaultdict(list)
    for route in router.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/admin/network/olt") and not path.startswith(
            "/admin/network/ont"
        ):
            continue
        methods = tuple(sorted(getattr(route, "methods", set()) or set()))
        endpoint = getattr(route, "endpoint", None)
        name = (
            f"{endpoint.__module__}.{endpoint.__name__}"
            if endpoint is not None
            else repr(route)
        )
        seen[(methods, path)].append(name)

    duplicates = [
        f"{methods} {path}: {', '.join(endpoints)}"
        for (methods, path), endpoints in sorted(seen.items())
        if len(endpoints) > 1
    ]
    assert not duplicates, "\n".join(duplicates)
