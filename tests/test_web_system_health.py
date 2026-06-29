from types import SimpleNamespace

from app.services import web_system_health


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, *results):
        self._results = list(results)
        self.rolled_back = False

    def execute(self, _statement):
        if not self._results:
            raise AssertionError("unexpected execute call")
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return _FakeResult(result)

    def rollback(self):
        self.rolled_back = True


def test_worker_health_summarizes_celery_service_details():
    service = SimpleNamespace(
        name="Celery",
        status="up",
        response_ms=12.5,
        details={
            "workers": ["celery@worker-a", "celery@worker-b"],
            "active_tasks": 3,
            "reserved_tasks": 2,
            "scheduled_tasks": 1,
            "queue_lengths": {"celery": 4, "billing": 0},
            "long_running_tasks_over_30m": [{"task_name": "slow.task"}],
        },
    )

    health = web_system_health._build_worker_health([service])

    assert health["status"] == "up"
    assert health["worker_count"] == 2
    assert health["active_tasks"] == 3
    assert health["queue_lengths"] == {"celery": 4, "billing": 0}
    assert health["long_running_tasks"] == [{"task_name": "slow.task"}]


def test_replication_health_reports_streaming_standby_as_up():
    db = _FakeDb(
        [
            {
                "application_name": "walreceiver",
                "client_addr": "75.119.157.91",
                "state": "streaming",
                "sync_state": "async",
                "bytes_behind": 0,
                "replay_lag_seconds": 0.018,
            }
        ],
        [
            {
                "slot_name": "standby_91",
                "active": True,
                "wal_status": "reserved",
                "retained_bytes": 384,
            }
        ],
    )

    health = web_system_health._build_replication_health(db)

    assert health["status"] == "up"
    assert health["summary"] == "1 standby connection(s) streaming."
    assert health["standbys"][0]["client_addr"] == "75.119.157.91"
    assert health["standbys"][0]["bytes_behind_display"] == "0.0 B"
    assert health["slots"][0]["slot_name"] == "standby_91"
    assert health["slots"][0]["active"] is True


def test_replication_health_flags_inactive_slot_without_standby():
    db = _FakeDb(
        [],
        [
            {
                "slot_name": "standby_91",
                "active": False,
                "wal_status": "reserved",
                "retained_bytes": 1024,
            }
        ],
    )

    health = web_system_health._build_replication_health(db)

    assert health["status"] == "degraded"
    assert health["summary"] == "Replication slot exists, but no standby is connected."
    assert health["standbys"] == []
    assert health["slots"][0]["retained_display"] == "1.0 KB"


def test_replication_health_rolls_back_on_query_error():
    db = _FakeDb(RuntimeError("permission denied"))

    health = web_system_health._build_replication_health(db)

    assert health["status"] == "unknown"
    assert health["summary"] == "Replication status unavailable."
    assert "permission denied" in health["error"]
    assert db.rolled_back is True
