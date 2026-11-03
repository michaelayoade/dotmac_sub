"""Typed relay configuration read from named environment variables only."""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

URL_VAR = "OUTBOX_RELAY_DATABASE_URL"
DEFAULT_HEARTBEAT_PATH = Path(tempfile.gettempdir()) / "outbox_relay.heartbeat"


class RelayConfigError(ValueError):
    """Configuration refused. Never carries a URL or credential value."""


def _int(env: Mapping[str, str], name: str, default: int, *, minimum: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RelayConfigError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RelayConfigError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class RelayConfig:
    database_url: str = ""
    batch_size: int = 50
    max_attempts: int = 8
    stale_lease_seconds: int = 300
    park_seconds: int = 300
    poll_interval_seconds: int = 2
    metrics_port: int = 9108
    heartbeat_path: Path = DEFAULT_HEARTBEAT_PATH

    def __repr__(self) -> str:  # the URL embeds a credential
        return "RelayConfig(<redacted>)"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> RelayConfig:
        url = env.get(URL_VAR, "").strip()
        if not url:
            raise RelayConfigError(
                f"{URL_VAR} is not set; the relay runs only with its own "
                "outbox_dispatcher credential and never falls back to DATABASE_URL"
            )
        if not url.startswith(("postgresql://", "postgresql+")):
            raise RelayConfigError(f"{URL_VAR} must be a PostgreSQL URL")
        heartbeat = env.get("OUTBOX_RELAY_HEARTBEAT_PATH", "").strip()
        return cls(
            database_url=url,
            batch_size=_int(env, "OUTBOX_RELAY_BATCH_SIZE", 50, minimum=1),
            max_attempts=_int(env, "OUTBOX_RELAY_MAX_ATTEMPTS", 8, minimum=1),
            stale_lease_seconds=_int(
                env, "OUTBOX_RELAY_STALE_LEASE_SECONDS", 300, minimum=30
            ),
            park_seconds=_int(env, "OUTBOX_RELAY_PARK_SECONDS", 300, minimum=10),
            poll_interval_seconds=_int(
                env, "OUTBOX_RELAY_POLL_INTERVAL_SECONDS", 2, minimum=1
            ),
            metrics_port=_int(env, "OUTBOX_RELAY_METRICS_PORT", 9108, minimum=1),
            heartbeat_path=Path(heartbeat) if heartbeat else DEFAULT_HEARTBEAT_PATH,
        )
