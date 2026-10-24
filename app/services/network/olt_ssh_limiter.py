"""Cross-process per-OLT SSH session limiter (G4 PR-A).

A Postgres *session* advisory-lock semaphore with N slots per OLT. Every
worker process, container and operator script that opens an OLT shell through
``olt_ssh._open_shell`` takes one slot first, so the OLT never sees more than
N concurrent Sub logins however many processes exist.

Why Postgres and not Redis: the lock belongs to a backend connection, so a
killed process frees its slot when the TCP connection drops (keepalives bound
this), there is no TTL or renewal thread that could silently over-admit, and
with the database down the limiter fails closed (no lock, no session).

Mechanics (mirrors ``app.tasks._postgres_lock``: one pinned connection per
held slot, autocommit, so no transaction is ever open while a slot is held):

* Slot key: ``blake2b("olt-ssh-slot:<olt>:<i>")`` as a signed bigint, namespaced
  so it cannot collide with the bare-``olt_id`` poller lock. A hash collision
  can only make two OLTs share a slot, which over-restricts and never widens.
* Priority WRITE > INTERACTIVE > BACKGROUND. BACKGROUND may use only the first
  ``N - reserved`` slots and never waits: it skips the cycle when busy. A
  waiting WRITE/INTERACTIVE caller holds a shared "intent" lock; BACKGROUND must
  first win the same lock exclusively, so a poller skips whenever anyone queues.
* A nested open for an OLT the current context already holds never blocks (it
  tries once, then raises :class:`OltSessionBusy`), which rules out
  hold-and-wait self-deadlock.
* :func:`olt_ssh_scope` holds one slot and one lazily opened session for a whole
  logical pass (a reconcile pass: pre-read, apply, verify) so it logs in once.

The whole limiter is a no-op when the application database is not PostgreSQL
(unit lane) or when the ``olt_ssh_limiter_enabled`` setting is off. Turning it
off restores unlimited sessions; that is Michael's lever.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import random
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

logger = logging.getLogger(__name__)

BUSY_PREFIX = "olt_busy:"
_SLOT_NAMESPACE = "olt-ssh-slot"
_INTENT_NAMESPACE = "olt-ssh-intent"
LIMITER_APPLICATION_NAME = "olt_ssh_limiter"
_CONFIG_TTL_SECONDS = 60.0
_MAX_SLOTS = 8


class OltSshPriority(IntEnum):
    """Lower value = higher priority."""

    WRITE = 0
    INTERACTIVE = 1
    BACKGROUND = 2


class OltSessionBusy(TimeoutError):
    """No SSH slot is free for this OLT within the wait budget.

    Subclasses ``TimeoutError`` so every existing
    ``except (SSHException, OSError, TimeoutError, ...)`` block treats it as
    "connection failed, nothing sent". That is safe because no command was ever
    issued: the exception is raised before any socket is opened.
    """


def busy_message(detail: str) -> str:
    return f"{BUSY_PREFIX} {detail}"


def is_busy_message(message: object) -> bool:
    return BUSY_PREFIX in str(message or "")


_AUTH_EOF_PATTERNS = (
    re.compile(r"transport shut down or saw EOF", re.IGNORECASE),
    re.compile(r"Error reading SSH protocol banner", re.IGNORECASE),
)


def is_auth_eof_message(message: object) -> bool:
    text = str(message or "")
    return any(pattern.search(text) for pattern in _AUTH_EOF_PATTERNS)


# ── Configuration ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LimiterConfig:
    enabled: bool = True
    max_default: int = 2
    max_ma5608t: int = 2
    max_ma5800: int = 4
    reserved_priority_slots: int = 1
    wait_write_seconds: int = 45
    wait_interactive_seconds: int = 15
    auth_eof_retry_delay_seconds: int = 7
    logout_on_close: bool = True
    release_grace_seconds_ma5608t: int = 2
    busy_max_deferrals: int = 5

    def limit_for(self, model_key: str) -> int:
        if model_key == "ma5800":
            value = self.max_ma5800
        elif model_key == "ma5608t":
            value = self.max_ma5608t
        else:
            value = self.max_default
        return max(1, min(int(value), _MAX_SLOTS))

    def reserved_for(self, limit: int) -> int:
        # Never reserve every slot: BACKGROUND must keep at least one.
        return max(0, min(int(self.reserved_priority_slots), limit - 1))

    def grace_for(self, model_key: str) -> float:
        if model_key == "ma5608t":
            return float(max(0, self.release_grace_seconds_ma5608t))
        return 0.0


_CONFIG_KEYS = {
    "olt_ssh_limiter_enabled": "enabled",
    "olt_ssh_max_sessions_default": "max_default",
    "olt_ssh_max_sessions_ma5608t": "max_ma5608t",
    "olt_ssh_max_sessions_ma5800": "max_ma5800",
    "olt_ssh_reserved_priority_slots": "reserved_priority_slots",
    "olt_ssh_slot_wait_write_seconds": "wait_write_seconds",
    "olt_ssh_slot_wait_interactive_seconds": "wait_interactive_seconds",
    "olt_ssh_auth_eof_retry_delay_seconds": "auth_eof_retry_delay_seconds",
    "olt_ssh_logout_on_close": "logout_on_close",
    "olt_ssh_release_grace_seconds_ma5608t": "release_grace_seconds_ma5608t",
    "olt_busy_max_deferrals": "busy_max_deferrals",
}

_config_lock = threading.Lock()
_config_cache: tuple[float, LimiterConfig] | None = None
_config_override: LimiterConfig | None = None


def set_config_override(config: LimiterConfig | None) -> None:
    """Pin the configuration (tests and multi-process harnesses)."""
    global _config_override, _config_cache
    with _config_lock:
        _config_override = config
        _config_cache = None


def _load_config_from_settings() -> LimiterConfig:
    from app.models.domain_settings import SettingDomain
    from app.services.db_session_adapter import db_session_adapter
    from app.services.settings_spec import resolve_values_atomic

    with db_session_adapter.read_session() as db:
        values = resolve_values_atomic(
            db, SettingDomain.network, list(_CONFIG_KEYS.keys())
        )
    defaults = LimiterConfig()
    updates: dict[str, Any] = {}
    for key, attr in _CONFIG_KEYS.items():
        if key not in values:
            continue
        default = getattr(defaults, attr)
        value = values[key]
        if isinstance(default, bool):
            updates[attr] = bool(value)
        else:
            try:
                updates[attr] = int(value)
            except (TypeError, ValueError):
                continue
    return LimiterConfig(**{**defaults.__dict__, **updates})


def get_config() -> LimiterConfig:
    """Settings with a 60 s in-process cache. Unreadable settings use defaults
    (limiter ON), never "off"."""
    global _config_cache
    if _config_override is not None:
        return _config_override
    now = time.monotonic()
    cached = _config_cache
    if cached is not None and now - cached[0] < _CONFIG_TTL_SECONDS:
        return cached[1]
    try:
        config = _load_config_from_settings()
    except Exception:
        logger.debug("olt_ssh_limiter_config_unreadable", exc_info=True)
        config = LimiterConfig()
    with _config_lock:
        _config_cache = (now, config)
    return config


def model_key_for(model: object) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "", str(model or "").lower())
    if "5800" in normalized:
        return "ma5800"
    if "5608" in normalized:
        return "ma5608t"
    return "default"


# ── Priority / deadline / holdings contexts ─────────────────────────────────

_priority_var: contextvars.ContextVar[OltSshPriority] = contextvars.ContextVar(
    "olt_ssh_priority", default=OltSshPriority.INTERACTIVE
)
_deadline_var: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "olt_ssh_deadline", default=None
)
_holdings_var: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "olt_ssh_holdings", default=()
)
_scopes_var: contextvars.ContextVar[tuple[tuple[str, Any], ...]] = (
    contextvars.ContextVar("olt_ssh_scopes", default=())
)


def current_priority() -> OltSshPriority:
    return _priority_var.get()


def current_deadline() -> float | None:
    return _deadline_var.get()


@contextmanager
def olt_ssh_priority(
    priority: OltSshPriority, *, deadline_monotonic: float | None = None
) -> Iterator[None]:
    """Declare the priority (and optional monotonic deadline) of OLT work."""
    token = _priority_var.set(priority)
    deadline_token = (
        _deadline_var.set(deadline_monotonic)
        if deadline_monotonic is not None
        else None
    )
    try:
        yield
    finally:
        _priority_var.reset(token)
        if deadline_token is not None:
            _deadline_var.reset(deadline_token)


# ── Lock primitives ─────────────────────────────────────────────────────────


def _signed_key(namespace: str, olt_key: str, index: int | None = None) -> int:
    label = f"{namespace}:{olt_key}" + (f":{index}" if index is not None else "")
    digest = hashlib.blake2b(label.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def slot_key(olt_key: str, index: int) -> int:
    return _signed_key(_SLOT_NAMESPACE, olt_key, index)


def intent_key(olt_key: str) -> int:
    return _signed_key(_INTENT_NAMESPACE, olt_key)


_engines: dict[str, Any] = {}
_engines_lock = threading.Lock()


def _database_url() -> str:
    from app.config import settings

    return str(settings.database_url or "")


def _is_postgres_url(url: str) -> bool:
    return url.startswith(("postgresql", "postgres:"))


def _limiter_engine(url: str) -> Any:
    """Dedicated engine: NullPool, autocommit, keepalives, short statement
    timeout. Separate from the app pools (small ``DB_POOL_SIZE`` workers must
    not starve because slots pin connections)."""
    with _engines_lock:
        engine = _engines.get(url)
        if engine is None:
            from sqlalchemy import create_engine
            from sqlalchemy.pool import NullPool

            engine = create_engine(
                url,
                poolclass=NullPool,
                isolation_level="AUTOCOMMIT",
                connect_args={
                    "application_name": LIMITER_APPLICATION_NAME,
                    "keepalives": 1,
                    "keepalives_idle": 30,
                    "keepalives_interval": 10,
                    "keepalives_count": 3,
                    "connect_timeout": 5,
                    "options": "-c statement_timeout=2000",
                },
            )
            _engines[url] = engine
        return engine


class SlotLease:
    """One held slot (or a null lease when the limiter is inactive)."""

    def __init__(
        self,
        *,
        conn: Any | None,
        key: int | None,
        olt_key: str,
        slot_index: int | None,
        wait_seconds: float = 0.0,
    ) -> None:
        self._conn = conn
        self._key = key
        self.olt_key = olt_key
        self.slot_index = slot_index
        self.wait_seconds = wait_seconds
        self.acquired_at = time.monotonic()
        self._lock = threading.Lock()
        self._released = False
        self._holding_token: contextvars.Token | None = None

    @classmethod
    def null(cls, olt_key: str) -> SlotLease:
        return cls(conn=None, key=None, olt_key=olt_key, slot_index=None)

    @property
    def released(self) -> bool:
        return self._released

    @property
    def is_null(self) -> bool:
        return self._conn is None and not self._released and self._key is None

    def release(self) -> None:
        """Release exactly once; never raises."""
        with self._lock:
            if self._released:
                return
            self._released = True
            conn, self._conn = self._conn, None
        if conn is None:
            return
        unlocked = False
        try:
            from sqlalchemy import text

            unlocked = bool(
                conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": self._key}
                ).scalar()
            )
        except Exception:
            logger.exception("olt_ssh_slot_unlock_failed olt=%s", self.olt_key)
        finally:
            try:
                if not unlocked:
                    # Closing the backend releases session locks regardless.
                    conn.invalidate()
                conn.close()
            except Exception:
                logger.debug("olt_ssh_slot_close_failed", exc_info=True)

    def __enter__(self) -> SlotLease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _slot_order(priority: OltSshPriority, limit: int, reserved: int) -> list[int]:
    if priority is OltSshPriority.BACKGROUND:
        return list(range(max(1, limit - reserved)))
    if priority is OltSshPriority.WRITE:
        # Writers take the reserved (highest-index) slots first.
        return list(range(limit - 1, -1, -1))
    return list(range(limit))


def acquire_slot(
    *,
    olt_key: str,
    limit: int,
    reserved: int,
    priority: OltSshPriority,
    wait_seconds: float,
    database_url: str | None = None,
) -> SlotLease:
    """Take one slot or raise :class:`OltSessionBusy`.

    ``database_url`` is injectable for multi-process tests; production passes
    ``None`` and uses the application database.
    """
    from sqlalchemy import text

    url = database_url or _database_url()
    if not _is_postgres_url(url):
        return SlotLease.null(olt_key)

    started = time.monotonic()
    try:
        conn = _limiter_engine(url).connect()
    except Exception as exc:
        # Fail closed: without the lock database there is no safe session.
        raise OltSessionBusy(
            busy_message("session limiter database unavailable")
        ) from exc

    def _try(sql: str, key: int) -> bool:
        return bool(conn.execute(text(sql), {"key": key}).scalar())

    try:
        intent = intent_key(olt_key)
        if priority is OltSshPriority.BACKGROUND:
            # Skip whenever a higher-priority caller is queued. A momentary
            # exclusive win is released at once.
            if not _try("SELECT pg_try_advisory_lock(:key)", intent):
                raise OltSessionBusy(
                    busy_message("a higher-priority request is waiting for a slot")
                )
            conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": intent})
            wait_seconds = 0.0

        order = _slot_order(priority, limit, reserved)
        deadline = started + max(0.0, wait_seconds)
        intent_held = False
        while True:
            for index in order:
                key = slot_key(olt_key, index)
                if _try("SELECT pg_try_advisory_lock(:key)", key):
                    if intent_held:
                        conn.execute(
                            text("SELECT pg_advisory_unlock_shared(:key)"),
                            {"key": intent},
                        )
                    return SlotLease(
                        conn=conn,
                        key=key,
                        olt_key=olt_key,
                        slot_index=index,
                        wait_seconds=time.monotonic() - started,
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OltSessionBusy(
                    busy_message(
                        f"all {limit} SSH session slot(s) for this OLT are in use"
                    )
                )
            if priority is not OltSshPriority.BACKGROUND and not intent_held:
                intent_held = _try("SELECT pg_try_advisory_lock_shared(:key)", intent)
            time.sleep(min(random.uniform(0.5, 1.5), remaining))  # noqa: S311 - jitter
    except BaseException:
        try:
            # Closing releases every session lock this connection holds.
            conn.invalidate()
            conn.close()
        except Exception:
            logger.debug("olt_ssh_slot_cleanup_failed", exc_info=True)
        raise


def acquire_for_olt(olt: Any) -> SlotLease:
    """Acquire the slot an ``_open_shell`` call needs, honouring priority,
    reentrancy and the off-switch. Records wait metrics."""
    from app.services.network import olt_ssh_metrics

    olt_key = str(getattr(olt, "id", "") or getattr(olt, "name", ""))
    config = get_config()
    if not config.enabled:
        return SlotLease.null(olt_key)
    model_key = model_key_for(getattr(olt, "model", None))
    limit = config.limit_for(model_key)
    reserved = config.reserved_for(limit)
    priority = current_priority()

    if priority is OltSshPriority.WRITE:
        budget = float(config.wait_write_seconds)
    elif priority is OltSshPriority.INTERACTIVE:
        budget = float(config.wait_interactive_seconds)
    else:
        budget = 0.0
    deadline = current_deadline()
    if deadline is not None:
        # Leave 30 s of the caller's deadline for the work itself.
        budget = min(budget, max(0.0, deadline - time.monotonic() - 30.0))
    if olt_key in _holdings_var.get():
        # Nested open: never wait while holding a slot (no hold-and-wait).
        budget = 0.0

    label = getattr(olt, "name", None) or olt_key
    try:
        lease = acquire_slot(
            olt_key=olt_key,
            limit=limit,
            reserved=reserved,
            priority=priority,
            wait_seconds=budget,
        )
    except OltSessionBusy:
        outcome = (
            "busy_skipped" if priority is OltSshPriority.BACKGROUND else "busy_timeout"
        )
        olt_ssh_metrics.record_open(label, priority.name.lower(), outcome)
        raise
    olt_ssh_metrics.record_wait(label, priority.name.lower(), lease.wait_seconds)
    lease._holding_token = _holdings_var.set((*_holdings_var.get(), olt_key))
    return lease


def mark_released(lease: SlotLease) -> None:
    """Release ``lease`` and drop its holding marker."""
    token, lease._holding_token = lease._holding_token, None
    lease.release()
    if token is not None:
        try:
            _holdings_var.reset(token)
        except ValueError:
            # Released from a different context (thread); holdings are per
            # logical operation, so the stale marker dies with its context.
            pass


# ── Scope: one slot + one session for a whole pass ──────────────────────────


class OltSshScope:
    """A held slot and one lazily opened session reused across a pass."""

    def __init__(self, *, olt_key: str, lease: SlotLease, label: str) -> None:
        self.olt_key = olt_key
        self.lease = lease
        self.label = label
        self._use_lock = threading.Lock()
        self._owner: int | None = None
        self._session: tuple[Any, Any, Any] | None = None
        self.opens = 0
        self.reuses = 0

    def checkout(self, opener: Any, normaliser: Any) -> tuple[Any, Any, Any]:
        """Return a view of the scope's session.

        ``opener`` opens a raw session inside the held slot. ``normaliser``
        returns the session to the post-login (user-mode) state and reports
        whether it worked; on failure the session is discarded and reopened
        once, still inside the same slot.
        """
        me = threading.get_ident()
        if self._owner == me:
            raise OltSessionBusy(
                busy_message("nested session request inside an active OLT scope")
            )
        budget = 30.0
        deadline = current_deadline()
        if deadline is not None:
            budget = max(0.0, min(budget, deadline - time.monotonic()))
        if not self._use_lock.acquire(timeout=budget):
            raise OltSessionBusy(busy_message("scope session is in use"))
        self._owner = me
        try:
            session = self._session
            if session is not None:
                if normaliser(session):
                    self.reuses += 1
                else:
                    self._discard()
                    session = None
            if session is None:
                session = opener()
                self._session = session
                self.opens += 1
            transport, channel, policy = session
            return _ScopedTransport(self, transport), channel, policy
        except BaseException:
            self._discard()
            self._owner = None
            self._use_lock.release()
            raise

    def checkin(self, *, discard: bool) -> None:
        if self._owner is None:
            return
        try:
            if discard:
                self._discard()
        finally:
            self._owner = None
            self._use_lock.release()

    def _discard(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                session[0].close_real()
            except Exception:
                try:
                    session[0].close()
                except Exception:
                    logger.debug("olt_ssh_scope_discard_failed", exc_info=True)

    def close(self) -> None:
        self._discard()


class _ScopedTransport:
    """View of the scope's transport: ``close()`` checks the session back in
    instead of closing it. Everything else delegates."""

    def __init__(self, scope: OltSshScope, transport: Any) -> None:
        self._scope = scope
        self._transport = transport
        self._returned = False

    def close(self) -> None:
        if self._returned:
            return
        self._returned = True
        discard = False
        try:
            state = getattr(self._transport, "session_clean", None)
            discard = not (callable(state) and state())
            if not discard and not self._transport.is_active():
                discard = True
        except Exception:
            discard = True
        self._scope.checkin(discard=discard)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._transport, name)


def active_scope(olt: Any) -> OltSshScope | None:
    olt_key = str(getattr(olt, "id", "") or getattr(olt, "name", ""))
    for key, scope in reversed(_scopes_var.get()):
        if key == olt_key:
            return scope
    return None


@contextmanager
def olt_ssh_scope(
    olt: Any,
    priority: OltSshPriority,
    *,
    deadline_monotonic: float | None = None,
) -> Iterator[OltSshScope | None]:
    """Hold one slot (and one lazily opened session) for a logical pass.

    Acquires the slot BEFORE any OLT I/O and raises :class:`OltSessionBusy` if
    none is free within the priority's wait budget. Yields ``None`` (no-op)
    when the limiter is inactive, so callers keep today's per-call behaviour.
    """
    config = get_config()
    existing = active_scope(olt)
    if existing is not None:
        yield existing
        return
    if not config.enabled or not _is_postgres_url(_database_url()):
        with olt_ssh_priority(priority, deadline_monotonic=deadline_monotonic):
            yield None
        return
    with olt_ssh_priority(priority, deadline_monotonic=deadline_monotonic):
        lease = acquire_for_olt(olt)
        olt_key = lease.olt_key
        scope = OltSshScope(
            olt_key=olt_key,
            lease=lease,
            label=str(getattr(olt, "name", None) or olt_key),
        )
        token = _scopes_var.set((*_scopes_var.get(), (olt_key, scope)))
        started = time.monotonic()
        try:
            yield scope
        finally:
            try:
                scope.close()
            finally:
                try:
                    _scopes_var.reset(token)
                except ValueError:
                    pass
                grace = config.grace_for(model_key_for(getattr(olt, "model", None)))
                if grace > 0:
                    time.sleep(grace)
                mark_released(lease)
                logger.info(
                    "olt_ssh_scope olt=%s priority=%s hold_ms=%d opens=%d reuses=%d",
                    scope.label,
                    priority.name.lower(),
                    int((time.monotonic() - started) * 1000),
                    scope.opens,
                    scope.reuses,
                )
