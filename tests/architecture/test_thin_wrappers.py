"""Architecture checks for thin web/api wrappers."""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROUTE_DIRS = [PROJECT_ROOT / "app" / "web", PROJECT_ROOT / "app" / "api"]
DISALLOWED_PATTERNS = [
    re.compile(r"\bdb\.query\("),
    re.compile(r"\bdb\.execute\("),
    re.compile(r"\bselect\("),
]


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for route_dir in ROUTE_DIRS:
        files.extend(path for path in route_dir.rglob("*.py") if path.is_file())
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
