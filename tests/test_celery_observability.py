from __future__ import annotations

from types import SimpleNamespace

from app import celery_app as celery_app_module


def test_task_extra_includes_request_context():
    task = SimpleNamespace(
        name="app.tasks.billing.run_invoice_cycle",
        request=SimpleNamespace(
            root_id="root-1",
            parent_id="parent-1",
            correlation_id="corr-1",
            retries=2,
            eta=None,
        ),
    )

    extra = celery_app_module._task_extra(task, "task-1", task_state="SUCCESS")

    assert extra["event"] == "celery_task"
    assert extra["task_id"] == "task-1"
    assert extra["task_name"] == "app.tasks.billing.run_invoice_cycle"
    assert extra["root_id"] == "root-1"
    assert extra["correlation_id"] == "corr-1"
    assert extra["task_state"] == "SUCCESS"


def test_task_signal_handlers_emit_structured_logs(caplog):
    task = SimpleNamespace(
        name="app.tasks.tr069.execute_pending_jobs",
        request=SimpleNamespace(
            root_id="root-2",
            parent_id=None,
            correlation_id="corr-2",
            retries=1,
            eta=None,
        ),
    )

    caplog.set_level("INFO")

    celery_app_module._log_task_prerun(
        task_id="task-2",
        task=task,
        args=("a", "b"),
        kwargs={"force": True},
    )
    celery_app_module._log_task_postrun(
        task_id="task-2",
        task=task,
        state="SUCCESS",
        retval={"queued": 3},
    )

    start_record = next(
        record for record in caplog.records if record.getMessage() == "celery_task_start"
    )
    complete_record = next(
        record
        for record in caplog.records
        if record.getMessage() == "celery_task_complete"
    )

    assert start_record.event == "celery_task"
    assert start_record.task_id == "task-2"
    assert start_record.arg_count == 2
    assert start_record.kwarg_keys == ["force"]
    assert complete_record.task_state == "SUCCESS"
    assert complete_record.result_type == "dict"


def test_enqueue_celery_task_uses_headers_and_logs(monkeypatch, caplog):
    captured = {}

    class _Result:
        id = "task-3"

    def _fake_send_task(name, args=None, kwargs=None, headers=None, **extra):
        captured["name"] = name
        captured["args"] = args
        captured["kwargs"] = kwargs
        captured["headers"] = headers
        captured["extra"] = extra
        return _Result()

    monkeypatch.setattr(celery_app_module.celery_app, "send_task", _fake_send_task)
    caplog.set_level("INFO")

    result = celery_app_module.enqueue_celery_task(
        "app.tasks.webhooks.deliver_webhook",
        args=["delivery-1"],
        correlation_id="webhook_event:event-1",
        source="event_webhook_handler",
    )

    assert result.id == "task-3"
    assert captured["headers"]["correlation_id"] == "webhook_event:event-1"
    assert captured["headers"]["source"] == "event_webhook_handler"
    queued_record = next(
        record for record in caplog.records if record.getMessage() == "celery_task_queued"
    )
    assert queued_record.task_id == "task-3"
    assert queued_record.correlation_id == "webhook_event:event-1"


def test_poll_all_olt_signals_uses_correlated_enqueue(monkeypatch):
    from app.tasks import olt_polling as olt_polling_module

    captured: list[dict[str, object]] = []

    class _FakeScalarResult:
        def __init__(self, items):
            self._items = items

        def all(self):
            return self._items

    class _FakeSession:
        def scalars(self, _stmt):
            return _FakeScalarResult(
                [
                    SimpleNamespace(id="olt-1", name="OLT One"),
                    SimpleNamespace(id="olt-2", name="OLT Two"),
                ]
            )

        def rollback(self):
            return None

        def close(self):
            return None

    def _fake_enqueue(task, *, args=None, kwargs=None, correlation_id=None, source=None, **extra):
        captured.append(
            {
                "task": task,
                "args": args,
                "kwargs": kwargs,
                "correlation_id": correlation_id,
                "source": source,
                "extra": extra,
            }
        )
        return SimpleNamespace(id=f"task-{len(captured)}")

    monkeypatch.setattr(olt_polling_module, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(olt_polling_module, "_mark_stale_onts_offline", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr("app.celery_app.enqueue_celery_task", _fake_enqueue)

    result = olt_polling_module.poll_all_olt_signals()

    assert result == {"olts_dispatched": 2, "stale_marked_offline": 1}
    assert captured[0]["args"] == ["olt-1"]
    assert captured[0]["kwargs"] is None
    assert captured[0]["correlation_id"] == "olt_poll:olt-1"
    assert captured[0]["source"] == "poll_all_olts"
    assert captured[1]["args"] == ["olt-2"]
    assert captured[1]["correlation_id"] == "olt_poll:olt-2"


def test_capture_all_olts_task_uses_correlated_enqueue(monkeypatch):
    from app.tasks import olt_capture as olt_capture_module

    captured: list[dict[str, object]] = []

    class _FakeScalarResult:
        def __init__(self, items):
            self._items = items

        def all(self):
            return self._items

    class _FakeSession:
        def scalars(self, _stmt):
            return _FakeScalarResult(
                [
                    SimpleNamespace(id="olt-10", name="OLT Ten"),
                    SimpleNamespace(id="olt-20", name="OLT Twenty"),
                ]
            )

        def close(self):
            return None

    def _fake_enqueue(task, *, args=None, kwargs=None, correlation_id=None, source=None, **extra):
        task_id = f"task-{len(captured) + 1}"
        captured.append(
            {
                "task": task,
                "args": args,
                "kwargs": kwargs,
                "correlation_id": correlation_id,
                "source": source,
                "extra": extra,
                "task_id": task_id,
            }
        )
        return SimpleNamespace(id=task_id)

    monkeypatch.setattr(olt_capture_module, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr("app.celery_app.enqueue_celery_task", _fake_enqueue)

    result = olt_capture_module.capture_all_olts_task(force=True)

    assert result["queued"] == 2
    assert captured[0]["args"] == ["olt-10"]
    assert captured[0]["kwargs"] == {"force": True}
    assert captured[0]["correlation_id"] == "olt_capture:olt-10"
    assert captured[0]["source"] == "capture_all_olts_task"
    assert result["tasks"][0]["task_id"] == "task-1"
    assert captured[1]["args"] == ["olt-20"]
    assert captured[1]["correlation_id"] == "olt_capture:olt-20"
