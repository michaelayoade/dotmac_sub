"""UDP syslog listener for receiving OLT events.

Provides an asyncio-based UDP server that receives syslog messages,
parses them, and routes events to appropriate handlers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.syslog.handlers import handle_ont_event
from app.syslog.health import (
    HealthSocketBinding,
    ListenerHealthReason,
    ListenerReadiness,
    bind_health_socket,
    default_socket_path,
)
from app.syslog.parsers import huawei_parser

logger = logging.getLogger(__name__)

# Configuration
SYSLOG_LISTEN_HOST = os.getenv("SYSLOG_LISTEN_HOST", "0.0.0.0")  # noqa: S104  # nosec B104
SYSLOG_LISTEN_PORT = int(os.getenv("SYSLOG_LISTEN_PORT", "514"))
SYSLOG_ENABLED = os.getenv("SYSLOG_ENABLED", "true").lower() in ("1", "true", "yes")


class SyslogProtocol(asyncio.DatagramProtocol):
    """UDP protocol handler for syslog messages."""

    def __init__(
        self,
        callback: Callable[[bytes, tuple[str, int]], Coroutine[object, object, None]],
        on_failure: Callable[[], None] | None = None,
    ) -> None:
        self.callback = callback
        self.on_failure = on_failure
        self.transport: asyncio.BaseTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle incoming UDP datagram."""
        asyncio.create_task(self.callback(data, addr))

    def connection_lost(self, exc: Exception | None) -> None:
        if self.on_failure is not None:
            self.on_failure()

    def error_received(self, exc: Exception) -> None:
        if self.on_failure is not None:
            self.on_failure()


class SyslogListener:
    """Asyncio UDP syslog listener service."""

    def __init__(
        self,
        host: str = SYSLOG_LISTEN_HOST,
        port: int = SYSLOG_LISTEN_PORT,
        *,
        health_socket_path: Path | None = None,
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
        self._generation = uuid4()
        self._last_loop_progress: float | None = None
        self._health_socket_path = health_socket_path or default_socket_path()
        self._health_server: asyncio.Server | None = None
        self._health_binding: HealthSocketBinding | None = None
        self._health_connections: set[asyncio.StreamWriter] = set()

    def _receiver_failed(self) -> None:
        self._running = False
        self._last_loop_progress = None

    def receiver_readiness(self) -> ListenerReadiness:
        """Only this receiver loop certifies its bound transport and progress."""
        now = time.monotonic()
        reason = ListenerHealthReason.READY
        if not self._running:
            reason = ListenerHealthReason.STOPPED
        elif self._transport is None or self._last_loop_progress is None:
            reason = ListenerHealthReason.STARTING
        elif self._transport.is_closing():
            reason = ListenerHealthReason.CLOSED
        elif not 0 <= now - self._last_loop_progress <= 3:
            reason = ListenerHealthReason.STALE
        return ListenerReadiness(reason, self._generation, now)

    async def _answer_health(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if len(self._health_connections) >= 8:
            writer.close()
            return
        self._health_connections.add(writer)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=1)
            challenge = raw.decode("ascii").strip()
            if len(challenge) != 32 or any(
                char not in "0123456789abcdef" for char in challenge
            ):
                return
            outcome = self.receiver_readiness()
            writer.write(
                (
                    json.dumps(
                        {
                            "challenge": challenge,
                            "generation": str(outcome.generation),
                            "observed_monotonic": outcome.observed_monotonic,
                            "reason": outcome.reason.value,
                        }
                    )
                    + "\n"
                ).encode("ascii")
            )
            await asyncio.wait_for(writer.drain(), timeout=1)
        except (
            OSError,
            TimeoutError,
            UnicodeError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            pass
        finally:
            self._health_connections.discard(writer)
            writer.close()

    async def run(self) -> None:
        """Start the syslog listener and run until stopped."""
        if not SYSLOG_ENABLED:
            logger.warning("Syslog listener is disabled via SYSLOG_ENABLED=false")
            return

        self._running = True
        self._generation = uuid4()
        self._last_loop_progress = None
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
                lambda: SyslogProtocol(
                    self._on_message_received, self._receiver_failed
                ),
                local_addr=(self.host, self.port),
            )

            self._health_binding = bind_health_socket(path=self._health_socket_path)
            self._health_server = await asyncio.start_unix_server(
                self._answer_health, sock=self._health_binding.socket, limit=128
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
                self._last_loop_progress = time.monotonic()
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
        self._running = False
        self._last_loop_progress = None
        if self._health_server is not None:
            self._health_server.close()
        for writer in tuple(self._health_connections):
            writer.close()
        self._health_connections.clear()
        if self._health_server is not None:
            await self._health_server.wait_closed()
            self._health_server = None
        if self._health_binding is not None:
            self._health_binding.close()
            self._health_binding = None

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
