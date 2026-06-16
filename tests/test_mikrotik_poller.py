import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from app.poller.mikrotik_poller import BandwidthPoller, QueueStats


def _run_async(coro):
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


class _FakeDevicePool:
    def __init__(self, device_id, subscription_id):
        self.device_id = device_id
        self.subscription_id = subscription_id

    async def poll_all(self, active_devices=None):
        yield self.device_id, [
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
        ]

    def resolve_subscription(self, device_id, queue_name):
        return self.subscription_id


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
