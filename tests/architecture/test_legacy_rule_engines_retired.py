"""The legacy ticket and inbox rule engines are retired (cleanup M2).

The tables and ORM models are kept until a later migration drops them, but no
application code may read or write them.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

RETIRED_FILES = (
    "app/services/support_automation.py",
    "app/services/support_automation_rules.py",
    "app/web/admin/support_automation.py",
    "app/services/team_inbox_automation.py",
    "templates/admin/support/automation",
)
RETIRED_NAMES = (
    "TicketAutomationRule",
    "InboxAutomationRule",
    "support_automation_rules",
    "team_inbox_automation",
    "_apply_automation_rules",
    "support_ticket_automation_rules",
    "inbox_automation_rules",
)
# The models (and the package export) are the only allowed references.
ALLOWED_REFERENCES = {
    "app/models/support.py",
    "app/models/team_inbox.py",
    "app/models/__init__.py",
}


def test_retired_engine_files_are_gone() -> None:
    for relative in RETIRED_FILES:
        assert not (ROOT / relative).exists(), relative


def test_no_application_code_references_retired_engines() -> None:
    pattern = re.compile("|".join(re.escape(name) for name in RETIRED_NAMES))
    offenders: list[str] = []
    for base in ("app", "templates"):
        for path in (ROOT / base).rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".html", ".js"}:
                continue
            relative = path.relative_to(ROOT).as_posix()
            if relative in ALLOWED_REFERENCES:
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                offenders.append(relative)
    assert offenders == []


def test_retired_engine_routes_are_not_mounted() -> None:
    from app.web.admin import router

    paths = {getattr(route, "path", "") for route in router.routes}
    assert "/admin/support/automation" not in paths
    assert not any("/support/automation" in path for path in paths)
    assert any(path.startswith("/admin/support/assignment-rules") for path in paths)
