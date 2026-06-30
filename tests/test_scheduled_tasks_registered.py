"""Guard: every scheduled task_name must resolve to a registered Celery task.

This catches the class of bug where a new task module is referenced from
``scheduler_config`` (so beat dispatches it) but never imported into
``app.tasks.__init__`` (so the worker rejects it as an unregistered task).

The check is static — it reads the ``task_name="..."`` literals declared in
``scheduler_config.py`` via AST, so it needs no database. Dynamic task names
(f-strings) are skipped; everything beat schedules from a constant must be
importable.
"""

import ast
from pathlib import Path

import app.services.scheduler_config as scheduler_config


def _declared_scheduled_task_names() -> set[str]:
    source = Path(scheduler_config.__file__).read_text()
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != "task_name":
            continue
        # Adjacent string literals (``"a." "b"``) are folded into one Constant.
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            names.add(node.value.value)
    return names


def test_every_scheduled_task_name_is_registered() -> None:
    import app.tasks  # noqa: F401 — side effect: register all task modules
    from app.celery_app import celery_app

    declared = _declared_scheduled_task_names()
    assert declared, "expected to find task_name= literals in scheduler_config"

    registered = set(celery_app.tasks.keys())
    missing = sorted(declared - registered)
    assert not missing, (
        "Scheduled task_name(s) declared in scheduler_config but NOT registered "
        "with Celery (the owning module is missing from app/tasks/__init__.py). "
        f"Beat will dispatch these and the worker will reject them: {missing}"
    )


def test_router_sync_capture_snapshots_registered() -> None:
    import app.tasks  # noqa: F401
    from app.celery_app import celery_app

    assert "router_sync.capture_scheduled_snapshots" in celery_app.tasks
