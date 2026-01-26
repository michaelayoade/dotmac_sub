"""
MikroTik Bandwidth Poller

High-frequency polling service that collects bandwidth samples from MikroTik
devices using the RouterOS API and publishes them to a Redis stream.
"""
import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Optional
from uuid import UUID

import redis.asyncio as redis
from routeros_api import RouterOsApiPool

from app.db import SessionLocal
from app.models.catalog import NasDevice, NasDeviceStatus, NasVendor
from app.services.queue_mapping import queue_mapping

logger = logging.getLogger(__name__)

# Configuration
POLL_INTERVAL_MS = int(os.getenv("BANDWIDTH_POLL_INTERVAL_MS", "1000"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_STREAM = os.getenv("BANDWIDTH_REDIS_STREAM", "bandwidth:samples")
POLLING_ENABLED = os.getenv("BANDWIDTH_POLLING_ENABLED", "true").lower() in ("1", "true", "yes")


@dataclass
class QueueStats:
    """Statistics for a single MikroTik queue."""
    name: str
    target: str
    rate_rx: int  # bytes per second
    rate_tx: int  # bytes per second
    bytes_rx: int
    bytes_tx: int
    packets_rx: int
    packets_tx: int


@dataclass
class BandwidthSample:
    """A single bandwidth sample to be published."""
    subscription_id: str
    nas_device_id: str
    queue_name: str
    rx_bps: int  # bits per second
    tx_bps: int  # bits per second
    sample_at: datetime


class MikroTikConnection:
    """
    Manages a persistent connection to a MikroTik device.

    Uses the RouterOS API to communicate with MikroTik devices.
    """

    def __init__(
        self,
        device_id: UUID,
        host: str,
        username: str,
        password: str,
        port: int = 8728,
    ):
        self.device_id = device_id
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self._pool: Optional[RouterOsApiPool] = None
        self._connection = None
        self._last_connected: Optional[datetime] = None
        self._consecutive_failures = 0

    async def connect(self) -> bool:
        """Establish connection to the device."""
        try:
            # RouterOS API is synchronous, run in executor
            loop = asyncio.get_event_loop()
            self._pool = await loop.run_in_executor(
                None,
                lambda: RouterOsApiPool(
                    self.host,
                    username=self.username,
                    password=self.password,
                    port=self.port,
                    plaintext_login=True,
                ),
            )
            self._connection = await loop.run_in_executor(
                None, self._pool.get_api
            )
            self._last_connected = datetime.now(timezone.utc)
            self._consecutive_failures = 0
            logger.info(f"Connected to MikroTik device {self.device_id} at {self.host}")
            return True
        except Exception as e:
            self._consecutive_failures += 1
            logger.error(f"Failed to connect to {self.host}: {e}")
            return False

    async def disconnect(self):
        """Close the connection."""
        if self._pool:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._pool.disconnect)
            except Exception as e:
                logger.warning(f"Error disconnecting from {self.host}: {e}")
            self._pool = None
            self._connection = None

    async def is_connected(self) -> bool:
        """Check if connection is still alive."""
        if not self._connection:
            return False
        try:
            # Try a simple command to verify connection
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self._connection.get_resource("/system/identity").get()
            )
            return True
        except Exception:
            return False

    async def get_queue_stats(self) -> list[QueueStats]:
        """
        Fetch queue statistics from the device.

        Returns list of QueueStats for all simple queues.
        """
        if not self._connection:
            if not await self.connect():
                return []

        try:
            loop = asyncio.get_event_loop()
            queues = await loop.run_in_executor(
                None,
                lambda: self._connection.get_resource("/queue/simple").get()
            )

            stats = []
            for q in queues:
                # Parse rate strings like "12345/67890" (rx/tx in bytes)
                rate = q.get("rate", "0/0").split("/")
                bytes_val = q.get("bytes", "0/0").split("/")
                packets = q.get("packets", "0/0").split("/")

                stats.append(QueueStats(
                    name=q.get("name", ""),
                    target=q.get("target", ""),
                    rate_rx=int(rate[0]) if rate[0].isdigit() else 0,
                    rate_tx=int(rate[1]) if len(rate) > 1 and rate[1].isdigit() else 0,
                    bytes_rx=int(bytes_val[0]) if bytes_val[0].isdigit() else 0,
                    bytes_tx=int(bytes_val[1]) if len(bytes_val) > 1 and bytes_val[1].isdigit() else 0,
                    packets_rx=int(packets[0]) if packets[0].isdigit() else 0,
                    packets_tx=int(packets[1]) if len(packets) > 1 and packets[1].isdigit() else 0,
                ))

            self._consecutive_failures = 0
            return stats

        except Exception as e:
            self._consecutive_failures += 1
            logger.error(f"Failed to get queue stats from {self.host}: {e}")
            # Reconnect on next attempt
            await self.disconnect()
            return []

    @property
    def should_retry(self) -> bool:
        """Check if we should attempt reconnection."""
        # Back off exponentially based on failures
        if self._consecutive_failures == 0:
            return True
        backoff_seconds = min(60, 2 ** self._consecutive_failures)
        if self._last_connected:
            elapsed = (datetime.now(timezone.utc) - self._last_connected).total_seconds()
            return elapsed >= backoff_seconds
        return True


class DevicePool:
    """
    Manages connections to all active NAS devices.

    Periodically refreshes the device list from the database.
    """

    def __init__(self, refresh_interval: int = 60):
        self._connections: dict[UUID, MikroTikConnection] = {}
        self._queue_mappings: dict[UUID, dict[str, UUID]] = {}
        self._refresh_interval = refresh_interval
        self._last_refresh: Optional[datetime] = None

    async def refresh_devices(self):
        """Refresh the list of devices from the database."""
        db = SessionLocal()
        try:
            devices = (
                db.query(NasDevice)
                .filter(
                    NasDevice.vendor == NasVendor.mikrotik,
                    NasDevice.status == NasDeviceStatus.active,
                    NasDevice.is_active.is_(True),
                )
                .all()
            )

            current_ids = set(self._connections.keys())
            new_ids = set()

            for device in devices:
                device_id = device.id
                new_ids.add(device_id)

                # Skip if already connected
                if device_id in self._connections:
                    continue

                # Create new connection if we have credentials
                if device.api_username and device.api_password and device.management_ip:
                    self._connections[device_id] = MikroTikConnection(
                        device_id=device_id,
                        host=device.management_ip,
                        username=device.api_username,
                        password=device.api_password,
                        port=device.management_port or 8728,
                    )

                # Load queue mappings for this device
                self._queue_mappings[device_id] = queue_mapping.get_device_mapping_dict(
                    db, device_id
                )

            # Remove connections for devices that are no longer active
            removed_ids = current_ids - new_ids
            for device_id in removed_ids:
                conn = self._connections.pop(device_id, None)
                if conn:
                    await conn.disconnect()
                self._queue_mappings.pop(device_id, None)

            self._last_refresh = datetime.now(timezone.utc)
            logger.info(f"Device pool refreshed: {len(self._connections)} devices")

        finally:
            db.close()

    def _should_refresh(self) -> bool:
        if not self._last_refresh:
            return True
        elapsed = (datetime.now(timezone.utc) - self._last_refresh).total_seconds()
        return elapsed >= self._refresh_interval

    async def poll_all(self) -> AsyncIterator[tuple[UUID, list[QueueStats]]]:
        """
        Poll all connected devices and yield queue stats.

        Yields tuples of (device_id, queue_stats).
        """
        if self._should_refresh():
            await self.refresh_devices()

        # Poll all devices concurrently
        async def poll_device(device_id: UUID, conn: MikroTikConnection):
            if not conn.should_retry:
                return device_id, []
            stats = await conn.get_queue_stats()
            return device_id, stats

        tasks = [
            poll_device(device_id, conn)
            for device_id, conn in self._connections.items()
        ]

        if not tasks:
            return

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Polling error: {result}")
                continue
            device_id, stats = result
            if stats:
                yield device_id, stats

    def resolve_subscription(self, device_id: UUID, queue_name: str) -> Optional[UUID]:
        """Resolve a queue name to a subscription ID."""
        mappings = self._queue_mappings.get(device_id, {})
        return mappings.get(queue_name)

    async def close(self):
        """Close all connections."""
        for conn in self._connections.values():
            await conn.disconnect()
        self._connections.clear()


class BandwidthPoller:
    """
    Main polling orchestrator.

    Runs a continuous polling loop, collecting bandwidth samples from
    all devices and publishing them to a Redis stream.
    """

    def __init__(self, poll_interval_ms: int = POLL_INTERVAL_MS):
        self.poll_interval_ms = poll_interval_ms
        self.device_pool = DevicePool()
        self._redis: Optional[redis.Redis] = None
        self._running = False
        self._poll_count = 0
        self._sample_count = 0

    async def _get_redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.from_url(REDIS_URL)
        return self._redis

    async def _publish_samples(self, samples: list[BandwidthSample]):
        """Publish samples to Redis stream."""
        if not samples:
            return

        r = await self._get_redis()

        # Publish as a batch to the stream
        for sample in samples:
            data = {
                "subscription_id": sample.subscription_id,
                "nas_device_id": sample.nas_device_id,
                "queue_name": sample.queue_name,
                "rx_bps": str(sample.rx_bps),
                "tx_bps": str(sample.tx_bps),
                "sample_at": sample.sample_at.isoformat(),
            }
            await r.xadd(REDIS_STREAM, data, maxlen=100000)

        self._sample_count += len(samples)

    async def _poll_once(self):
        """Execute a single polling cycle."""
        sample_time = datetime.now(timezone.utc)
        samples = []

        async for device_id, queue_stats in self.device_pool.poll_all():
            for qs in queue_stats:
                subscription_id = self.device_pool.resolve_subscription(
                    device_id, qs.name
                )
                if subscription_id:
                    # Convert bytes/s to bits/s (multiply by 8)
                    samples.append(BandwidthSample(
                        subscription_id=str(subscription_id),
                        nas_device_id=str(device_id),
                        queue_name=qs.name,
                        rx_bps=qs.rate_rx * 8,
                        tx_bps=qs.rate_tx * 8,
                        sample_at=sample_time,
                    ))

        if samples:
            await self._publish_samples(samples)

        self._poll_count += 1

    async def run(self):
        """
        Run the polling loop.

        Polls all devices at the configured interval and publishes
        bandwidth samples to Redis.
        """
        if not POLLING_ENABLED:
            logger.warning("Bandwidth polling is disabled")
            return

        self._running = True
        interval_seconds = self.poll_interval_ms / 1000.0

        logger.info(
            f"Starting bandwidth poller with {interval_seconds}s interval"
        )

        try:
            while self._running:
                start = time.monotonic()

                try:
                    await self._poll_once()
                except Exception as e:
                    logger.error(f"Polling error: {e}")

                # Calculate sleep time to maintain consistent interval
                elapsed = time.monotonic() - start
                sleep_time = max(0, interval_seconds - elapsed)

                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)

                # Log stats periodically
                if self._poll_count % 60 == 0:
                    logger.info(
                        f"Poller stats: {self._poll_count} polls, "
                        f"{self._sample_count} samples"
                    )

        finally:
            await self.stop()

    async def stop(self):
        """Stop the polling loop and cleanup."""
        self._running = False
        await self.device_pool.close()
        if self._redis:
            await self._redis.close()
        logger.info("Bandwidth poller stopped")


async def main():
    """Entry point for the poller service."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    poller = BandwidthPoller()

    # Handle shutdown signals
    loop = asyncio.get_event_loop()

    def handle_signal():
        logger.info("Received shutdown signal")
        asyncio.create_task(poller.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    await poller.run()


if __name__ == "__main__":
    asyncio.run(main())
