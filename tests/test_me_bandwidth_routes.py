"""Bearer-auth /me/bandwidth routes.

These mirror the cookie-only /bandwidth/my/* web routes so the mobile app (which
authenticates with a Bearer token) can reach live throughput at all. The routes
resolve the caller's active subscription and return subscriber-perspective
download/upload.
"""

import asyncio
from types import SimpleNamespace

from app.api import me


def _run(coro):
    return asyncio.run(coro)


PRINCIPAL = {"principal_type": "subscriber", "subscriber_id": "sub-uuid"}


def test_stats_resolves_subscription_and_maps_directions(monkeypatch):
    captured = {}

    def fake_active(db, principal):
        captured["principal"] = principal
        return SimpleNamespace(id="subscription-1")

    async def fake_stats(db, subscription_id, period):
        captured["subscription_id"] = subscription_id
        captured["period"] = period
        # rx/tx in NAS perspective; to_subscriber_directions maps tx->download.
        return {
            "current_rx_bps": 1_000_000,
            "current_tx_bps": 8_000_000,
            "peak_rx_bps": 2_000_000,
            "peak_tx_bps": 20_000_000,
        }

    monkeypatch.setattr(me.bandwidth_samples, "get_user_active_subscription", fake_active)
    monkeypatch.setattr(me.bandwidth_samples, "get_bandwidth_stats", fake_stats)

    out = _run(me.my_bandwidth_stats(period="24h", db=None, principal=PRINCIPAL))

    assert captured["subscription_id"] == "subscription-1"
    assert captured["period"] == "24h"
    # Subscriber-perspective: download == tx, upload == rx.
    assert out["download_bps"] == 8_000_000
    assert out["upload_bps"] == 1_000_000
    assert out["peak_download_bps"] == 20_000_000
    assert out["peak_upload_bps"] == 2_000_000


def test_series_resolves_subscription_and_adds_directions(monkeypatch):
    def fake_active(db, principal):
        return SimpleNamespace(id="subscription-1")

    async def fake_series(db, subscription_id, start_at, end_at, interval):
        return {
            "data": [{"rx_bps": 1_000_000, "tx_bps": 8_000_000}],
            "total": 1,
            "source": "victoriametrics",
        }

    monkeypatch.setattr(me.bandwidth_samples, "get_user_active_subscription", fake_active)
    monkeypatch.setattr(me.bandwidth_samples, "get_bandwidth_series", fake_series)

    out = _run(
        me.my_bandwidth_series(
            start_at=None, end_at=None, interval="auto", db=None, principal=PRINCIPAL
        )
    )

    point = out["data"][0]
    assert point["download_bps"] == 8_000_000
    assert point["upload_bps"] == 1_000_000
