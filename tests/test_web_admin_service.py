import pytest

from app.models.provisioning import ServiceOrder, ServiceOrderStatus
from app.services import module_manager as module_manager_service
from app.services import settings_spec
from app.services import web_admin as web_admin_service


def _reset_sidebar_cache() -> None:
    web_admin_service._sidebar_stats_cached_at = 0.0
    web_admin_service._sidebar_stats_cache = None


@pytest.fixture(autouse=True)
def _clear_sidebar_cache():
    _reset_sidebar_cache()
    yield
    _reset_sidebar_cache()


def test_count_open_service_orders_counts_only_non_terminal(db_session, subscriber):
    rows = [
        ServiceOrder(subscriber_id=subscriber.id, status=ServiceOrderStatus.draft),
        ServiceOrder(subscriber_id=subscriber.id, status=ServiceOrderStatus.submitted),
        ServiceOrder(subscriber_id=subscriber.id, status=ServiceOrderStatus.active),
        ServiceOrder(subscriber_id=subscriber.id, status=ServiceOrderStatus.failed),
        ServiceOrder(subscriber_id=subscriber.id, status=ServiceOrderStatus.canceled),
    ]
    db_session.add_all(rows)
    db_session.commit()

    assert web_admin_service._count_open_service_orders(db_session) == 2


def test_get_sidebar_stats_uses_short_ttl_cache(db_session, monkeypatch):
    _reset_sidebar_cache()
    calls = {"count": 0}

    def _fake_count(_db):
        calls["count"] += 1
        return 7

    def _fake_setting(_db, _domain, key):
        mapping = {
            "sidebar_logo_url": "/logo.svg",
            "sidebar_logo_dark_url": "/logo-dark.svg",
            "favicon_url": "/favicon.ico",
        }
        return mapping.get(key, "")

    monkeypatch.setattr(web_admin_service, "_count_open_service_orders", _fake_count)
    monkeypatch.setattr(settings_spec, "resolve_value", _fake_setting)
    monkeypatch.setattr(
        module_manager_service,
        "load_module_states",
        lambda _db: {"billing": True},
    )
    monkeypatch.setattr(
        module_manager_service,
        "load_feature_states",
        lambda _db: {"payments": True},
    )

    first = web_admin_service.get_sidebar_stats(db_session)
    second = web_admin_service.get_sidebar_stats(db_session)

    assert first["service_orders"] == 7
    assert second["service_orders"] == 7
    assert calls["count"] == 1
