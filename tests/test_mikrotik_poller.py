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


def test_connect_disconnects_pool_when_get_api_fails(monkeypatch):
    """Regression: a failure after the pool logs in must release the session.

    Constructing RouterOsApiPool opens the socket and logs in, so a session
    exists on the router. If get_api() then fails, connect() must disconnect the
    pool (exactly once) instead of orphaning the session — the production
    symptom was stale snap-api sessions accumulating on garki-core.
    """
    disconnect_calls = []

    class _Pool:
        def __init__(self, host, **kwargs):
            # Construction "succeeds": the API session now exists on the router.
            pass

        def get_api(self):
            raise RuntimeError("router busy: get_api timed out")

        def disconnect(self):
            disconnect_calls.append(True)

    monkeypatch.setattr(mikrotik_poller, "RouterOsApiPool", _Pool)

    conn = MikroTikConnection(
        device_id=uuid4(),
        host="192.0.2.10",
        username="admin",
        password="secret",
    )

    assert _run_async(conn.connect()) is False
    # The established session was released, not leaked.
    assert disconnect_calls == [True]
    assert conn._pool is None
    assert conn._connection is None


def test_connect_success_does_not_disconnect(monkeypatch):
    """The success path returns True and must NOT disconnect the pool."""
    disconnect_calls = []

    class _Pool:
        def __init__(self, host, **kwargs):
            pass

        def get_api(self):
            return object()

        def disconnect(self):
            disconnect_calls.append(True)

    monkeypatch.setattr(mikrotik_poller, "RouterOsApiPool", _Pool)

    conn = MikroTikConnection(
        device_id=uuid4(),
        host="192.0.2.10",
        username="admin",
        password="secret",
    )

    assert _run_async(conn.connect()) is True
    assert disconnect_calls == []
    assert conn._connection is not None
    assert conn._pool is not None


def test_connect_handles_none_pool(monkeypatch):
    """A None pool from construction is handled without crashing."""
    monkeypatch.setattr(
        mikrotik_poller, "RouterOsApiPool", lambda *args, **kwargs: None
    )

    conn = MikroTikConnection(
        device_id=uuid4(),
        host="192.0.2.10",
        username="admin",
        password="secret",
    )

    assert _run_async(conn.connect()) is False
    assert conn._pool is None
    assert conn._connection is None


def test_sanitize_exc_names_blank_exceptions_and_redacts_password():
    assert _sanitize_exc(TimeoutError()) == "TimeoutError"
    assert (
        _sanitize_exc(RuntimeError("failure =password=secret "))
        == "failure =password=<redacted> "
    )
