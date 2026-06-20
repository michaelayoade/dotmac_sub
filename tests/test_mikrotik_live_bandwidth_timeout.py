"""mikrotik-live bandwidth handles a NAS timeout gracefully (#51).

A RouterOS API connect/read timeout used to bubble up to a 500 on
GET /api/v1/bandwidth/mikrotik-live/{id}. It should now return a graceful
"unavailable" payload instead.
"""

import routeros_api
from routeros_api.exceptions import RouterOsApiConnectionError

from app.services.nas import _mikrotik


class _StubDevice:
    id = "11111111-1111-1111-1111-111111111111"
    name = "Test NAS"


def test_live_bandwidth_graceful_on_routeros_timeout(monkeypatch):
    class _TimingOutPool:
        def __init__(self, *args, **kwargs):
            pass

        def get_api(self):
            raise RouterOsApiConnectionError("timed out")

        def disconnect(self):
            pass

    monkeypatch.setattr(routeros_api, "RouterOsApiPool", _TimingOutPool)
    monkeypatch.setattr(
        _mikrotik,
        "_mikrotik_routeros_auth",
        lambda device: ("10.0.0.1", 8728, "admin", "secret"),
    )

    result = _mikrotik.get_mikrotik_pppoe_live_bandwidth(_StubDevice(), login="cust1")

    assert result["online"] is False
    assert result["available"] is False
    assert result["error"] == "nas_unreachable"
    assert result["current_rx_bps"] == 0.0
    assert result["download_bps"] == 0.0
    assert result["nas_device_name"] == "Test NAS"
