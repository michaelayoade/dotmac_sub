from types import SimpleNamespace

from app.services import worker_control


def test_restart_worker_target_rejects_unknown_target(monkeypatch):
    monkeypatch.setenv("CELERY_WORKER_RESTART_ENABLED", "true")

    result = worker_control.restart_worker_target("unknown-worker")

    assert result.ok is False
    assert result.message == "Worker restart target is not allowed."


def test_restart_worker_target_respects_disable_flag(monkeypatch):
    monkeypatch.setenv("CELERY_WORKER_RESTART_ENABLED", "false")

    result = worker_control.restart_worker_target("celery-worker")

    assert result.ok is False
    assert result.message == "Worker restart is disabled by configuration."


def test_restart_worker_target_runs_configured_command(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("CELERY_WORKER_RESTART_ENABLED", "true")
    monkeypatch.setenv(
        "CELERY_WORKER_RESTART_COMMAND",
        "docker compose restart {target}",
    )
    monkeypatch.setattr(worker_control.subprocess, "run", fake_run)

    result = worker_control.restart_worker_target("celery-worker-billing")

    assert result.ok is True
    assert result.message == "Restart requested for celery-worker-billing."
    assert captured["command"] == [
        "docker",
        "compose",
        "restart",
        "celery-worker-billing",
    ]
    assert captured["kwargs"]["timeout"] == 20.0
