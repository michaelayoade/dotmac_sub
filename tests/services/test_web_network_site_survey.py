from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services import web_network_site_survey as site_survey_service


def _request_stub() -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(actor_id="actor-123"),
        cookies={},
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"user-agent": "pytest", "x-request-id": "req-1"},
    )


def test_create_survey_logs_event(monkeypatch):
    request = _request_stub()
    db = MagicMock()
    survey = SimpleNamespace(id="survey-1", name="Campus Survey")

    monkeypatch.setattr(
        site_survey_service.ws_service.wireless_surveys,
        "create_from_form",
        lambda *_args, **_kwargs: survey,
        raising=False,
    )
    monkeypatch.setattr(
        site_survey_service.ws_service.wireless_surveys,
        "build_post_create_redirect",
        lambda survey_id, lat, lon: f"/redirect/{survey_id}?lat={lat}&lon={lon}",
        raising=False,
    )
    logged = {}
    monkeypatch.setattr(site_survey_service, "_actor_id", lambda _req: "user-999")

    def _capture_log(**kwargs):
        logged.update(kwargs)

    monkeypatch.setattr(site_survey_service, "log_audit_event", _capture_log)

    redirect_url = site_survey_service.create_survey(
        request,
        db,
        name="Campus Survey",
        description=None,
        frequency_mhz=5180.0,
        default_antenna_height_m=10.0,
        default_tx_power_dbm=18.0,
        project_id=None,
        subscriber_id=None,
        initial_lat=1.0,
        initial_lon=2.0,
    )

    assert redirect_url == "/redirect/survey-1?lat=1.0&lon=2.0"
    assert logged["entity_type"] == "site_survey"
    assert logged["entity_id"] == "survey-1"
    assert logged["metadata"] == {"name": "Campus Survey"}


def test_delete_point_returns_parent_redirect(monkeypatch):
    db = MagicMock()
    point = SimpleNamespace(
        id="point-1",
        survey_id="survey-2",
        name="Tower",
        point_type=SimpleNamespace(value="custom"),
    )
    monkeypatch.setattr(
        site_survey_service.ws_service.survey_points,
        "get",
        lambda _db, point_id: point,
        raising=False,
    )
    deleted = {}
    monkeypatch.setattr(
        site_survey_service.ws_service.survey_points,
        "delete",
        lambda _db, point_id: deleted.setdefault("point_id", point_id),
        raising=False,
    )
    monkeypatch.setattr(site_survey_service, "_actor_id", lambda _req: "user-1")
    monkeypatch.setattr(
        site_survey_service,
        "log_audit_event",
        lambda **kwargs: deleted.setdefault("log", kwargs),
    )

    request = _request_stub()
    redirect_url = site_survey_service.delete_point(request, db, point_id="point-1")

    assert redirect_url == "/admin/network/site-survey/survey-2"
    assert deleted["point_id"] == "point-1"
    assert deleted["log"]["metadata"]["point"] == "Tower"
