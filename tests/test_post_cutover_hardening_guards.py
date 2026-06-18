import re
from pathlib import Path

from app.models.scheduler import ScheduledTask, ScheduleType

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = REPO_ROOT / "app"


# Known, TRACKED transitional gates. Each is allowed to exist TODAY but has a
# named exit criterion (see docs/POST_CUTOVER_HARDENING.md). This set is a
# RATCHET — it may only SHRINK:
#   - A new (file, gate) not listed here fails ``test_no_new_source_of_truth_gates``.
#   - A listed entry whose gate has been removed from code fails
#     ``test_known_gate_debts_are_not_stale`` — forcing the allow-list to shrink
#     as debts are paid, so it can never ossify.
#
# Exit criteria:
#   trust_ipam                 → delete once IPAM drift is verified ~0 and the
#                                connectivity reconciler projects from the
#                                IPAssignment set unconditionally (step 2b).
#   _shadow_write_access_state → delete once group-routing access-state is
#                                canonical and the per-user Mikrotik-Address-List
#                                shadow is retired.
KNOWN_GATE_DEBTS = frozenset(
    {
        ("app/services/connectivity_reconciler.py", "trust_ipam"),
        ("app/services/events/handlers/enforcement.py", "_shadow_write_access_state"),
    }
)

_GATE_PATTERNS = (
    re.compile(r"\btrust_[A-Za-z0-9_]*\b"),
    re.compile(r"\b[A-Za-z0-9_]*_cutover_enabled\b"),
    re.compile(r"\b_?shadow_write_[A-Za-z0-9_]*\b"),
    re.compile(r"\buse_splynx_[A-Za-z0-9_]*\b"),
)


def _app_python_files() -> list[Path]:
    return sorted(
        path for path in APP_ROOT.rglob("*.py") if "__pycache__" not in path.parts
    )


def test_no_splynx_task_is_registered():
    """Code-level guard: no retired Splynx task may be registered in the Celery
    app. This fails the moment a ``splynx_sync``-style task module is recreated
    and imported — unlike a DB query, which only sees the (empty) test DB.

    NB: only holds once the Splynx task modules are deleted AND dropped from
    ``app/tasks/__init__``. Commit this guard WITH that removal, not before.
    """
    from app.celery_app import celery_app

    splynx = sorted(name for name in celery_app.tasks if "splynx" in name.lower())
    assert splynx == [], (
        f"Retired Splynx tasks still registered in Celery: {splynx}. "
        "Delete the task module and its app/tasks/__init__ import."
    )


def test_enabled_schedules_do_not_target_splynx_tasks(db_session):
    """Belt to the registry guard: no ENABLED ScheduledTask row targets a Splynx
    task. (In CI this sees only the test DB; the registry guard above is the one
    with teeth against code reintroduction.)"""
    disabled_legacy = ScheduledTask(
        name="disabled legacy Splynx task",
        task_name="app.tasks.splynx_sync.run_incremental_sync",
        schedule_type=ScheduleType.interval,
        interval_seconds=3600,
        args_json=[],
        kwargs_json={},
        enabled=False,
    )
    db_session.add(disabled_legacy)
    db_session.flush()

    rows = (
        db_session.query(ScheduledTask)
        .filter(ScheduledTask.enabled.is_(True))
        .filter(ScheduledTask.task_name.ilike("%splynx%"))
        .all()
    )

    assert rows == []


def test_no_new_source_of_truth_gates_are_added_to_app_code():
    """No new cutover/source-of-truth gate may appear in app/ beyond the tracked
    debts in KNOWN_GATE_DEBTS. Post-cutover the system should reconcile to a
    desired state, not branch on a gate that picks old-vs-new truth."""
    violations: list[str] = []
    for path in _app_python_files():
        rel_path = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        for pattern in _GATE_PATTERNS:
            for match in pattern.finditer(text):
                if (rel_path, match.group(0)) not in KNOWN_GATE_DEBTS:
                    violations.append(f"{rel_path}: {match.group(0)}")
    assert violations == []


def test_known_gate_debts_are_not_stale():
    """Ratchet: every allow-listed debt must still exist in code. When a debt is
    paid (gate removed), its entry must be removed from KNOWN_GATE_DEBTS too —
    otherwise this fails. Keeps the allow-list shrinking, never ossifying."""
    stale: list[tuple[str, str]] = []
    for rel_path, token in KNOWN_GATE_DEBTS:
        path = REPO_ROOT / rel_path
        if not path.exists() or token not in path.read_text(encoding="utf-8"):
            stale.append((rel_path, token))
    assert stale == [], (
        f"KNOWN_GATE_DEBTS entries no longer present in code (remove them): {stale}"
    )
