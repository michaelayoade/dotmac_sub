from app.services import web_admin_dashboard


class _ImmediateThread:
    def __init__(self, *, target, **_kwargs):
        self.target = target

    def start(self):
        self.target()


def test_expired_dashboard_cache_returns_stale_while_refreshing(monkeypatch):
    stale = {"snapshot": "stale"}
    fresh = {"snapshot": "fresh"}
    monkeypatch.setattr(web_admin_dashboard, "_dashboard_global_cache", stale)
    monkeypatch.setattr(web_admin_dashboard, "_dashboard_global_cached_at", 0.0)
    monkeypatch.setattr(web_admin_dashboard, "_dashboard_global_refreshing", False)
    monkeypatch.setattr(
        web_admin_dashboard, "_DASHBOARD_GLOBAL_STALE_WHILE_REVALIDATE", True
    )
    monkeypatch.setattr(
        web_admin_dashboard, "_DASHBOARD_GLOBAL_MAX_STALE_SECONDS", float("inf")
    )
    monkeypatch.setattr(web_admin_dashboard, "Thread", _ImmediateThread)
    monkeypatch.setattr(
        web_admin_dashboard, "_build_dashboard_global_context", lambda _db: fresh
    )

    class _Session:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("app.db.SessionLocal", lambda: _Session())

    result = web_admin_dashboard._get_cached_dashboard_global_context(object())

    assert result is stale
    assert web_admin_dashboard._dashboard_global_cache is fresh
    assert web_admin_dashboard._dashboard_global_refreshing is False


def test_device_metric_dashboard_index_matches_query_predicate():
    from app.models.network_monitoring import DeviceMetric

    index = next(
        item
        for item in DeviceMetric.__table__.indexes
        if item.name == "ix_device_metrics_rx_bps_recorded_at"
    )

    assert [column.name for column in index.columns] == ["recorded_at"]
    assert "metric_type = 'rx_bps' AND value > 0" in str(
        index.dialect_options["postgresql"]["where"]
    )
