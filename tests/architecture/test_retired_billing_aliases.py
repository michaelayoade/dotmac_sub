from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_retired_dunning_task_alias_stays_removed() -> None:
    tasks = _read("app/tasks/collections.py")
    routes = _read("app/celery_app.py")

    assert "app.tasks.collections.run_dunning" not in tasks
    assert "app.tasks.collections.run_dunning" not in routes


def test_retired_prepaid_control_alias_stays_removed() -> None:
    registry = _read("app/services/control_registry.py")
    settings = _read("app/services/settings_spec.py")

    assert '"prepaid_balance_enforcement_enabled"' not in registry
    assert 'key="prepaid_balance_enforcement_enabled"' not in settings
