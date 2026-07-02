import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import app.poller.mikrotik_poller as mikrotik_poller
from app.poller.mikrotik_poller import (
    BandwidthPoller,
    MikroTikConnection,
    QueueStats,
    _sanitize_exc,
)


def _run_async(coro):
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


class _FakeDevicePool:
    def __init__(self, device_id, subscription_id):
        self.device_id = device_id
        self.subscription_id = subscription_id

    async def poll_all(self, active_devices=None):
        yield (
            self.device_id,
            [
                QueueStats(
                    name="<pppoe-100024890>",
                    target="",
                    rate_rx=6_000_000,
                    rate_tx=6_000_000,
                    bytes_rx=0,
                    bytes_tx=0,
                    packets_rx=0,
                    packets_tx=0,
                )
            ],
        )

    def resolve_subscription(self, device_id, queue_name):
        return self.subscription_id

    def resolve_speed(self, subscription_id):
        # No plan cap in this fixture -> no clamp applied.
        return (0, 0)


def test_mikrotik_queue_rates_are_stored_as_bits_per_second():
    device_id = uuid4()
    subscription_id = uuid4()
    poller = BandwidthPoller()
    poller.device_pool = _FakeDevicePool(device_id, subscription_id)
    published = []

    async def capture_samples(samples):
        published.extend(samples)

    poller._publish_samples = capture_samples

    _run_async(poller._poll_once())

    assert len(published) == 1
    assert published[0].rx_bps == 6_000_000
    assert published[0].tx_bps == 6_000_000


def test_mikrotik_connection_does_not_pass_unsupported_socket_timeout(monkeypatch):
    captured_kwargs = {}

    class _Pool:
        def __init__(self, host, **kwargs):
            captured_kwargs.update(kwargs)

        def get_api(self):
            return object()

    monkeypatch.setattr(mikrotik_poller, "RouterOsApiPool", _Pool)

    conn = MikroTikConnection(
        device_id=uuid4(),
        host="192.0.2.10",
        username="admin",
        password="secret",
    )

    assert _run_async(conn.connect()) is True
    assert "socket_timeout" not in captured_kwargs


def test_sanitize_exc_names_blank_exceptions_and_redacts_password():
    assert _sanitize_exc(TimeoutError()) == "TimeoutError"
    assert (
        _sanitize_exc(RuntimeError("failure =password=secret "))
        == "failure =password=<redacted> "
    )
