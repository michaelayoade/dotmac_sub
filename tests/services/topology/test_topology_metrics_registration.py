"""Topology metrics export task is registered + routed + scheduled."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.celery_app import celery_app
from app.services import scheduler_config

TASK = "app.tasks.topology_metrics.export_topology_metrics"


def test_task_registered():
    import app.tasks  # noqa: F401

    assert TASK in celery_app.tasks


def test_task_routed_to_ingestion():
    assert celery_app.conf.task_routes[TASK] == {"queue": "ingestion"}


def test_task_exported():
    import app.tasks as tasks

    assert "export_topology_metrics" in tasks.__all__
    assert hasattr(tasks, "export_topology_metrics")


def test_interval_setting_is_registered():
    from app.models.domain_settings import SettingDomain
    from app.services.settings_spec import SETTINGS_SPECS

    spec = next(
        (
            s
            for s in SETTINGS_SPECS
            if s.domain == SettingDomain.network_monitoring
            and s.key == "topology_metrics_interval_seconds"
        ),
        None,
    )
    assert spec is not None
    assert spec.default == 900
    assert spec.min_value == 300


def test_beat_row_registered_with_default_interval(monkeypatch):
    """build_beat_schedule syncs a topology_metrics_export row (900s default,
    300s floor)."""
    monkeypatch.setenv("GIS_SYNC_ENABLED", "false")
    monkeypatch.delenv("TOPOLOGY_METRICS_INTERVAL_SECONDS", raising=False)

    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
    mock_session.query.return_value.filter.return_value.all.return_value = []
    mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

    with patch.object(scheduler_config, "SessionLocal", return_value=mock_session):
        with patch.object(
            scheduler_config.integration_service,
            "list_interval_jobs",
            return_value=[],
        ):
            scheduler_config.build_beat_schedule()

    scheduled_calls = mock_session.add.call_args_list
    assert any(
        getattr(call.args[0], "name", None) == "topology_metrics_export"
        and getattr(call.args[0], "task_name", None) == TASK
        and getattr(call.args[0], "interval_seconds", None) == 900
        for call in scheduled_calls
    )
