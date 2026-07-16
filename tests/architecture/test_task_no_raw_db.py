"""app/tasks/ must not grow raw DB access — tasks orchestrate; owners write.

The route thin-wrapper guard (`test_thin_wrappers.py`) only scans app/web + app/api,
which is exactly how `app/tasks/radius.py` grew raw psycopg + its own SQL policy
outside any owner. This ratchets the task layer: no NEW task may open a raw psycopg
connection or drive a cursor.

`_postgres_lock.py` is the sanctioned pinned-connection advisory-lock helper (the one
place that legitimately owns a raw connection). `radius.py` is tracked migration debt
— its RADIUS writers are being collapsed onto `access.radius_projection`; it shrinks
out of the baseline when that lands.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TASKS_DIR = PROJECT_ROOT / "app" / "tasks"

# The sanctioned raw-connection owner: the pinned-connection advisory-lock helper.
SANCTIONED = {"_postgres_lock.py"}

# Shrink-only migration debt. Remove an entry once its raw DB access is gone;
# adding one means a task grew a hand-rolled DB path instead of calling an owner.
RAW_DB_BASELINE = {"radius.py"}

RAW_DB_PATTERNS = [
    re.compile(r"\bpsycopg2?\.connect\("),
    re.compile(r"\.cursor\(\)"),
    re.compile(r"\bcur\.execute\("),
]


def _tasks_with_raw_db() -> set[str]:
    offenders: set[str] = set()
    for path in TASKS_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(TASKS_DIR))
        if rel in SANCTIONED:
            continue
        text = path.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in RAW_DB_PATTERNS):
            offenders.add(rel)
    return offenders


def test_no_new_task_uses_raw_db_access() -> None:
    offenders = _tasks_with_raw_db()
    new = sorted(offenders - RAW_DB_BASELINE)
    assert not new, (
        "task(s) opening a raw psycopg connection / cursor outside an owner "
        "service. Call the owning domain service (which owns the transaction) "
        "instead of hand-rolling DB access in a Celery task:\n  " + "\n  ".join(new)
    )


def test_task_raw_db_baseline_only_shrinks() -> None:
    offenders = _tasks_with_raw_db()
    stale = sorted(RAW_DB_BASELINE - offenders)
    assert not stale, (
        "these tasks no longer use raw DB access — remove them from "
        "RAW_DB_BASELINE so the migration debt reflects reality:\n  "
        + "\n  ".join(stale)
    )
