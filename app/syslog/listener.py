"""UDP syslog listener for receiving OLT events.

Provides an asyncio-based UDP server that receives syslog messages,
parses them, and routes events to appropriate handlers.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import UTC, datetime

from app.syslog.handlers import handle_ont_event
from app.syslog.parsers import huawei_parser

logger = logging.getLogger(__name__)

# Configuration
SYSLOG_LISTEN_HOST = os.getenv("SYSLOG_LISTEN_HOST", "0.0.0.0")  # noqa: S104  # nosec B104
SYSLOG_LISTEN_PORT = int(os.getenv("SYSLOG_LISTEN_PORT", "514"))
SYSLOG_ENABLED = os.getenv("SYSLOG_ENABLED", "true").lower() in ("1", "true", "yes")


class SyslogProtocol(asyncio.DatagramProtocol):
    """UDP protocol handler for syslog messages."""

    def __init__(self, callback):
        self.callback = callback
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]):
        """Handle incoming UDP datagram."""
        asyncio.create_task(self.callback(data, addr))


class SyslogListener:
    """Asyncio UDP syslog listener service."""

    def __init__(
        self,
        host: str = SYSLOG_LISTEN_HOST,
        port: int = SYSLOG_LISTEN_PORT,
    ) -> None:
        self.host = host
        self.port = port
        self._running = False
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: SyslogProtocol | None = None
        self._event_count = 0
        self._ont_event_count = 0
        self._start_time: datetime | None = None
        self._last_stats_log_count = 0

    async def run(self) -> None:
        """Start the syslog listener and run until stopped."""
        if not SYSLOG_ENABLED:
            logger.warning("Syslog listener is disabled via SYSLOG_ENABLED=false")
            return

        self._running = True
        self._start_time = datetime.now(UTC)
        self._event_count = 0
        self._ont_event_count = 0

        logger.info(
            "syslog_listener_starting",
            extra={
                "host": self.host,
                "port": self.port,
            },
        )

        try:
            loop = asyncio.get_event_loop()
            self._transport, self._protocol = await loop.create_datagram_endpoint(
                lambda: SyslogProtocol(self._on_message_received),
                local_addr=(self.host, self.port),
            )

            logger.info(
                "syslog_listener_started",
                extra={
                    "host": self.host,
                    "port": self.port,
                },
            )

            # Run until stopped
            while self._running:
                await asyncio.sleep(1)
                self._maybe_log_stats()

        except PermissionError:
            logger.error(
                "syslog_listener_permission_denied",
                extra={
                    "port": self.port,
                    "message": "Port 514 requires root privileges. Use a higher port or run as root.",
                },
            )
        except OSError as e:
            logger.error(
                "syslog_listener_bind_failed",
                extra={
                    "host": self.host,
                    "port": self.port,
                    "error": str(e),
                },
            )
        except Exception as e:
            logger.exception(
                "syslog_listener_error",
                extra={"error": str(e)},
            )
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Stop the syslog listener and cleanup."""
        if not self._running:
            return

        self._running = False

        if self._transport:
            self._transport.close()
            self._transport = None

        uptime = 0.0
        if self._start_time:
            uptime = (datetime.now(UTC) - self._start_time).total_seconds()

        logger.info(
            "syslog_listener_stopped",
            extra={
                "events_received": self._event_count,
                "ont_events_processed": self._ont_event_count,
                "uptime_seconds": round(uptime, 1),
            },
        )

    async def _on_message_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Process a received syslog message.

        Args:
            data: Raw UDP packet data
            addr: Tuple of (ip, port) of sender
        """
        self._event_count += 1
        source_ip = addr[0]

        try:
            # Parse the syslog message
            msg = huawei_parser.parse_syslog(data, source_ip=source_ip)
            if not msg:
                return

            # Check for ONT events
            event = huawei_parser.parse_ont_event(msg)
            if event:
                self._ont_event_count += 1
                handle_ont_event(event)

        except Exception as e:
            logger.warning(
                "syslog_message_processing_error",
                extra={
                    "source_ip": source_ip,
                    "error": str(e),
                    "data_preview": data[:100].decode("utf-8", errors="replace"),
                },
            )

    def _maybe_log_stats(self) -> None:
        """Log statistics periodically (every 100 events)."""
        if self._event_count - self._last_stats_log_count < 100:
            return

        if not self._start_time:
            return

        self._last_stats_log_count = self._event_count
        uptime = (datetime.now(UTC) - self._start_time).total_seconds()
        eps = self._event_count / uptime if uptime > 0 else 0

        logger.info(
            "syslog_listener_stats",
            extra={
                "events_received": self._event_count,
                "ont_events_processed": self._ont_event_count,
                "uptime_seconds": round(uptime, 1),
                "events_per_second": round(eps, 2),
            },
        )


async def main() -> None:
    """Entry point for the syslog listener service."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    listener = SyslogListener()
    loop = asyncio.get_event_loop()

    def handle_shutdown_signal():
        logger.info("Received shutdown signal")
        asyncio.create_task(listener.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_shutdown_signal)

    await listener.run()


if __name__ == "__main__":
    asyncio.run(main())
