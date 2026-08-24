from types import SimpleNamespace
from uuid import uuid4

from app.services.network import support_monitoring


class _Db:
    pass


def test_support_monitoring_keeps_radius_and_ont_provenance_separate(monkeypatch):
    session = SimpleNamespace(framed_ip_address="10.0.0.2", last_update=None)
    monkeypatch.setattr(
        support_monitoring.radius_sessions,
        "resolve_subscriber_radius_sessions",
        lambda *_: SimpleNamespace(
            sessions=(session,), primary_session=session, is_online=True
        ),
    )
    monkeypatch.setattr(
        support_monitoring.subscriber_ont_adapter,
        "get_subscriber_onts",
        lambda *_: [
            SimpleNamespace(
                ont_id="ont-1", serial_number="SERIAL", olt_status="offline"
            )
        ],
    )
    result = support_monitoring.project_support_monitoring(
        _Db(), support_monitoring.SupportMonitoringQuery(uuid4(), True)
    )
    assert result.status is support_monitoring.SupportMonitoringStatus.available
    assert result.radius is not None
    assert result.radius.source == "network.radius_sessions"
    assert result.onts[0].source == "network.ont_status"
    assert result.onts[0].effective_state == "offline"


def test_no_radius_or_ont_evidence_is_no_data(monkeypatch):
    monkeypatch.setattr(
        support_monitoring.radius_sessions,
        "resolve_subscriber_radius_sessions",
        lambda *_: SimpleNamespace(sessions=(), primary_session=None, is_online=False),
    )
    monkeypatch.setattr(
        support_monitoring.subscriber_ont_adapter, "get_subscriber_onts", lambda *_: []
    )
    result = support_monitoring.project_support_monitoring(
        _Db(), support_monitoring.SupportMonitoringQuery(uuid4(), True)
    )
    assert result.status is support_monitoring.SupportMonitoringStatus.no_data
    assert result.radius is None and result.onts == ()


def test_monitoring_refuses_unauthorized_and_preserves_unavailable(monkeypatch):
    denied = support_monitoring.project_support_monitoring(
        _Db(), support_monitoring.SupportMonitoringQuery(uuid4(), False)
    )
    assert denied.status is support_monitoring.SupportMonitoringStatus.unauthorized
    monkeypatch.setattr(
        support_monitoring.radius_sessions,
        "resolve_subscriber_radius_sessions",
        lambda *_: (_ for _ in ()).throw(RuntimeError("down")),
    )
    unavailable = support_monitoring.project_support_monitoring(
        _Db(), support_monitoring.SupportMonitoringQuery(uuid4(), True)
    )
    assert unavailable.status is support_monitoring.SupportMonitoringStatus.unavailable
    assert unavailable.radius is None and unavailable.onts == ()


def test_ont_only_observation_never_fabricates_radius_diagnostics(monkeypatch):
    monkeypatch.setattr(
        support_monitoring.radius_sessions,
        "resolve_subscriber_radius_sessions",
        lambda *_: SimpleNamespace(sessions=(), primary_session=None, is_online=False),
    )
    monkeypatch.setattr(
        support_monitoring.subscriber_ont_adapter,
        "get_subscriber_onts",
        lambda *_: [
            SimpleNamespace(ont_id="ont-1", serial_number=None, olt_status="online")
        ],
    )
    result = support_monitoring.project_support_monitoring(
        _Db(), support_monitoring.SupportMonitoringQuery(uuid4(), True)
    )
    assert result.status is support_monitoring.SupportMonitoringStatus.available
    assert result.radius is None
    assert result.onts[0].effective_state == "online"
    assert not hasattr(result, "los")
    assert not hasattr(result, "outage")
    assert not hasattr(result, "cpe_diagnostics")
    assert not hasattr(result, "sla")
