from types import SimpleNamespace

from app.services import web_admin_dashboard


def test_infrastructure_service_summary_groups_operational_states():
    services = [
        SimpleNamespace(status="up"),
        SimpleNamespace(status="healthy"),
        SimpleNamespace(status="degraded"),
        SimpleNamespace(status="warning"),
        SimpleNamespace(status="down"),
        SimpleNamespace(status="failed"),
        SimpleNamespace(status="not_configured"),
    ]

    summary = web_admin_dashboard._build_infrastructure_service_summary(services)

    assert summary == {
        "total": 7,
        "up": 2,
        "degraded": 2,
        "down": 2,
        "unknown": 1,
    }


def test_dashboard_infrastructure_health_uses_short_lived_cache(monkeypatch):
    calls = 0
    services = [SimpleNamespace(status="up")]

    def fake_check_all_services(_db):
        nonlocal calls
        calls += 1
        return services

    monkeypatch.setattr(web_admin_dashboard, "_dashboard_infrastructure_cache", None)
    monkeypatch.setattr(web_admin_dashboard, "_dashboard_infrastructure_cached_at", 0.0)
    monkeypatch.setattr(
        web_admin_dashboard, "_DASHBOARD_INFRASTRUCTURE_TTL_SECONDS", 60.0
    )
    monkeypatch.setattr(
        web_admin_dashboard.infrastructure_health_service,
        "check_all_services",
        fake_check_all_services,
    )
    monkeypatch.setattr(
        web_admin_dashboard.web_system_health_service,
        "_build_worker_health",
        lambda _services: "workers",
    )

    first = web_admin_dashboard._load_dashboard_infrastructure_health(object())
    second = web_admin_dashboard._load_dashboard_infrastructure_health(object())

    assert first == second
    assert calls == 1
    assert first[1] == "workers"
    assert first[2]["up"] == 1
