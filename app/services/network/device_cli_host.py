"""Host adapters for ``dotmac-device-cli`` (ADR-0077): the ONLY importer.

The package is stateless dialogue and profile knowledge. It performs no I/O and
never sees a credential. Everything that touches the world stays Sub's:

* transport, credentials, host-key verification, the per-OLT slot and the
  logout/grace/metrics machinery all remain in ``olt_ssh._open_shell`` /
  ``_manage_transport``;
* this module adapts the already-authenticated paramiko channel to the package's
  ``ShellChannel``, supplies a Postgres advisory ``LockStore``, resolves the
  profile from the OLT row's configured vendor/model/firmware, and maps typed
  package results back onto Sub's legacy ``(bool, message, payload)`` tuples.

S1 wires exactly ONE read, default OFF, falling back to the legacy path only
BEFORE anything is sent: ``list_onts`` -> ``olt_ssh_ont.status.
get_registered_ont_serials``. (``version`` is not wired: its production callers
are the firmware upgrade pre-check and post-verify.)

The kill switch and selection live in ``device_cli_gate``, which does not import
the package: callers import THIS module lazily, only after the gate passes, and
treat an ``ImportError`` as "legacy". This module then only does the profile
checks (resolved profile claims the op, is not ``unverified``, agrees with Sub's
SSH policy, paging and legacy command). Once the package path has started,
``Unreadable`` and adapter errors are returned as failures. There is NEVER a
legacy re-login in the same call: it would double logins on shelves that drop
concurrent sessions and would mask the fail-closed signal.

Logout ownership (the sharp edge): when the package owns the dialogue it also
owns ``logout()``. Sub's transport close must then type nothing a second time,
so the package path ends with :func:`_close_package_owned`, which marks the
managed holder closed, records the logout outcome once, and calls
``transport.close_real`` (close + slot release with the model grace, no
``quit``).
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from dotmac_device_cli import (
    CliSession,
    Clock,
    LockStore,
    MonotonicClock,
    NoProfile,
    Ok,
    OntInventory,
    Priority,
    Profile,
    ProfileError,
    ProfileRegistry,
    Reason,
    Unreadable,
    Unsupported,
    builtin_registry,
    list_onts,
)
from dotmac_device_cli.core.dialect import dialect_for
from dotmac_device_cli.core.limiter import (
    LimiterConfig as PackageLimiterConfig,
)
from dotmac_device_cli.core.limiter import (
    SlotLease as PackageSlotLease,
)
from dotmac_device_cli.core.limiter import (
    acquire_slot as package_acquire_slot,
)
from dotmac_device_cli.core.limiter import wait_budget as package_wait_budget
from paramiko.channel import Channel
from paramiko.ssh_exception import SSHException

from app.models.network import OLTDevice
from app.services.network import device_cli_gate as gate
from app.services.network.olt_ssh_limiter import OltSshPriority
from app.services.network.olt_ssh_ont._common import RegisteredOntEntry
from app.services.network.olt_vendor_adapters import OltSshPolicy

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)


class _ClosableTransport(Protocol):
    def close(self) -> None: ...


#: Re-exported from the gate (the gate holds the flags and never imports the package).
OP_LIST_ONTS = gate.OP_LIST_ONTS

# Connection-class failures the legacy reader already catches around the open.
_OPEN_ERRORS = (SSHException, EOFError, OSError, TimeoutError, ValueError)
# Failures of the session itself after a successful open. ValueError and
# RuntimeError there are programming errors (adapter_error), not connectivity.
_SESSION_CONNECTION_ERRORS = (SSHException, EOFError, OSError, TimeoutError)

# Per-port cap, as legacy (``_run_huawei_paged_cmd(timeout_sec=min(30, ...))``).
_PER_PORT_SECONDS = 30

# ── channel adapter ──────────────────────────────────────────────────────────


class ParamikoShellChannel:
    """Satisfies the package ``ShellChannel`` over an authenticated paramiko channel.

    Idle reads must surface as ``TimeoutError`` (the engine keys on it). paramiko
    raises ``socket.timeout``, which IS ``TimeoutError`` on every supported Python
    (>=3.11), so it passes through unchanged; the test suite pins that alias so
    the contract does not silently depend on it.
    Sub's own ``_instrument_channel`` wrappers (clean-tail tracking) stay in
    place on the underlying channel; running both trackers is harmless, running
    both LOGOUTS is not (see :func:`_close_package_owned`).
    """

    def __init__(self, channel: Channel) -> None:
        self._channel = channel
        #: Monotonic instant after which reads report EOF. The package ops take no
        #: per-call deadline, so the host bounds each port here (an overrun ends
        #: the session as CLOSED: the read fails and nothing more is typed).
        self.deadline: float | None = None

    def send(self, data: str) -> int:
        sent = self._channel.send(data)
        return sent if isinstance(sent, int) else len(data)

    def recv(self, nbytes: int) -> bytes:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            return b""
        chunk = self._channel.recv(nbytes)
        if isinstance(chunk, str):
            return chunk.encode("utf-8")
        return bytes(chunk or b"")

    def recv_ready(self) -> bool:
        return bool(self._channel.recv_ready())

    def settimeout(self, seconds: float) -> None:
        self._channel.settimeout(seconds)

    @property
    def closed(self) -> bool:
        return bool(getattr(self._channel, "closed", False))


# ── lock-store adapter (Postgres advisory locks) ─────────────────────────────


class PgAdvisoryLockStore:
    """One pinned autocommit connection = one lease (the package's ``LockStore``).

    The four statements are the ones ``olt_ssh_limiter.acquire_slot`` runs, so
    old and new code contend on the same locks in the same database.
    """

    def __init__(self, conn: Connection) -> None:
        self._conn: Connection | None = conn

    def _scalar(self, sql: str, key: int) -> bool:
        from sqlalchemy import text

        if self._conn is None:
            raise RuntimeError("lock store is closed")  # fail closed, never no-op
        return bool(self._conn.execute(text(sql), {"key": key}).scalar())

    def try_lock(self, key: int) -> bool:
        return self._scalar("SELECT pg_try_advisory_lock(:key)", key)

    def unlock(self, key: int) -> None:
        self._scalar("SELECT pg_advisory_unlock(:key)", key)

    def try_lock_shared(self, key: int) -> bool:
        return self._scalar("SELECT pg_try_advisory_lock_shared(:key)", key)

    def unlock_shared(self, key: int) -> None:
        self._scalar("SELECT pg_advisory_unlock_shared(:key)", key)

    def close(self) -> None:
        """Invalidate then close: closing the backend frees every session lock
        even when an explicit unlock failed (mirrors ``SlotLease.release``)."""
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.invalidate()
        except Exception:
            logger.debug("device_cli_lock_store_invalidate_failed", exc_info=True)
        try:
            conn.close()
        except Exception:
            logger.debug("device_cli_lock_store_close_failed", exc_info=True)


def pg_store_factory(database_url: str | None = None) -> Callable[[], LockStore]:
    """A factory yielding one ``PgAdvisoryLockStore`` per lease.

    Reuses Sub's dedicated limiter engine (NullPool, AUTOCOMMIT, keepalives,
    ``statement_timeout``, ``LIMITER_APPLICATION_NAME``). There is NO null lane:
    a non-PostgreSQL URL or an unreachable database raises, which the package
    turns into ``DeviceBusy`` (fail closed: no lock, no session).
    """
    from app.services.network import olt_ssh_limiter

    def factory() -> LockStore:
        url = database_url or olt_ssh_limiter._database_url()
        if not olt_ssh_limiter._is_postgres_url(url):
            raise RuntimeError("device-cli limiter requires PostgreSQL")
        return PgAdvisoryLockStore(olt_ssh_limiter._limiter_engine(url).connect())

    return factory


def device_key_for(olt: OLTDevice) -> str:
    """The lock-key identity: ``str(olt.id)`` and nothing else.

    Legacy ``acquire_for_olt`` falls back to the OLT name when the id is empty;
    the package path refuses instead of deriving different slots.
    """
    key = str(getattr(olt, "id", "") or "")
    if not key:
        raise ValueError("device-cli requires a non-empty OLT id for slot keys")
    return key


def _to_package_priority(priority: OltSshPriority) -> Priority:
    return Priority(int(priority))


def acquire_device_slot(
    olt: OLTDevice,
    profile: Profile,
    *,
    priority: OltSshPriority | None = None,
    store_factory: Callable[[], LockStore] | None = None,
    clock: Clock | None = None,
) -> PackageSlotLease:
    """Take one slot through the package policy over the shared advisory locks.

    Sub's ``olt_ssh_*`` settings stay authoritative; the profile can only LOWER
    the slot count. NOT wired into any production path in S1 (``_open_shell``
    still takes the slot through ``olt_ssh_limiter``); it is proven by the
    lock-key goldens and the cross-engine slot-sharing tests so the later swap
    cannot change slot identity.
    """
    from app.services.network import olt_ssh_limiter

    device_key = device_key_for(olt)
    config = olt_ssh_limiter.get_config()
    model_key = olt_ssh_limiter.model_key_for(getattr(olt, "model", None))
    limit = PackageLimiterConfig.limit_for(
        profile.limits, host_max=config.limit_for(model_key)
    )
    reserved = min(config.reserved_for(limit), max(0, limit - 1))
    effective = priority if priority is not None else olt_ssh_limiter.current_priority()
    package_priority = _to_package_priority(effective)
    nested = device_key in olt_ssh_limiter._holdings_var.get()
    wait = package_wait_budget(
        PackageLimiterConfig(
            wait_write_seconds=config.wait_write_seconds,
            wait_interactive_seconds=config.wait_interactive_seconds,
        ),
        package_priority,
        now=time.monotonic(),
        deadline=olt_ssh_limiter.current_deadline(),
        nested=nested,
    )
    return package_acquire_slot(
        store_factory or pg_store_factory(),
        device_key=device_key,
        limit=limit,
        reserved=reserved,
        priority=package_priority,
        wait_seconds=wait,
        clock=clock or MonotonicClock(),
    )


# ── profile resolution and cross-checks ──────────────────────────────────────


@functools.lru_cache(maxsize=1)
def _registry() -> ProfileRegistry:
    return builtin_registry()


def resolve_profile(olt: OLTDevice) -> Profile | NoProfile:
    """Resolve from the OLT row's CONFIGURED vendor/model/firmware only.

    ``detect()`` never selects a profile; an ambiguous match is treated as
    unresolved. No generic or neighbouring profile is ever borrowed.
    """
    firmware = getattr(olt, "firmware_version", None) or getattr(
        olt, "software_version", None
    )
    vendor = str(getattr(olt, "vendor", None) or "")
    model = str(getattr(olt, "model", None) or "")
    try:
        return _registry().resolve(vendor, model, str(firmware or ""))
    except ProfileError:
        return NoProfile(vendor, model, str(firmware or ""))


def ssh_policy_mismatch(profile: Profile, policy: OltSshPolicy) -> str | None:
    """Drift between the profile ``[ssh]`` data and Sub's ``OltSshPolicy``.

    Sub applies its own policy; any disagreement keeps the read on legacy (and is
    a test failure for the shipped profiles), never a runtime choice.
    """
    for field in ("kex", "host_key_types", "ciphers", "macs"):
        ours = tuple(getattr(policy, field, ()) or ())
        theirs = tuple(getattr(profile.ssh, field))
        if ours != theirs:
            return f"ssh {field} differ"
    return None


def legacy_inventory_command(olt: OLTDevice, fsp: str) -> str:
    from app.services.network.huawei_command_profiles import (
        get_huawei_command_profile,
    )

    return get_huawei_command_profile(olt).display_ont_status_inventory(fsp)


def package_inventory_command(profile: Profile, fsp: str) -> str:
    dialect = dialect_for(profile.dialect)
    if dialect is None:
        raise ValueError(f"no dialect {profile.dialect!r}")
    return dialect.compose(OP_LIST_ONTS, profile, dialect.parse_ref(fsp))


@dataclass(frozen=True, slots=True)
class ReadPlan:
    """A committed decision to use the package for this call (nothing sent yet)."""

    operation: str
    profile: Profile
    olt_label: str


def _label(olt: OLTDevice) -> str:
    return str(getattr(olt, "name", "") or getattr(olt, "id", "") or "")


def paging_mismatch(profile: Profile, policy: OltSshPolicy) -> str | None:
    """Legacy turns paging off only with ``paging_off_command``; the profile only
    with a supported ``off_command``. Disagreement keeps the read on legacy."""
    if (policy.paging_off_command is not None) != profile.paging.off_supported:
        return "paging-off handling differs"
    return None


def plan_read(
    olt: OLTDevice, operation: str, *, fsps: Iterable[str] = ()
) -> ReadPlan | None:
    """Profile checks for a call the gate already selected. ``None`` = legacy.

    Every refusal here happens before any login, so falling back is safe (no
    double login). Flags, OLT, priority and scope are the gate's job.
    """
    from app.services.network import olt_ssh_metrics

    label = _label(olt)
    try:
        profile = resolve_profile(olt)
        if isinstance(profile, NoProfile):
            olt_ssh_metrics.record_device_cli_event(
                "profile_unresolved", label, operation
            )
            return None
        if not profile.claims(operation):
            olt_ssh_metrics.record_device_cli_event("unsupported", label, operation)
            return None
        if profile.verification.status == "unverified":
            olt_ssh_metrics.record_device_cli_event(
                "profile_unverified", label, operation
            )
            return None
        from app.services.network import olt_ssh as core

        policy = core.resolve_policy(olt)
        mismatch = ssh_policy_mismatch(profile, policy) or paging_mismatch(
            profile, policy
        )
        if mismatch is None and operation == OP_LIST_ONTS:
            for fsp in fsps:
                if package_inventory_command(profile, fsp) != legacy_inventory_command(
                    olt, fsp
                ):
                    mismatch = "inventory command differs from the legacy command"
                    break
        if mismatch is not None:
            logger.warning(
                "device_cli_profile_mismatch olt=%s op=%s profile=%s detail=%s",
                label,
                operation,
                profile.id,
                mismatch,
            )
            olt_ssh_metrics.record_device_cli_event(
                "profile_mismatch", label, operation
            )
            return None
    except Exception:
        # Planning happens before anything is sent, so legacy is safe here.
        logger.exception("device_cli_plan_failed olt=%s op=%s", label, operation)
        olt_ssh_metrics.record_device_cli_event("plan_error", label, operation)
        return None
    return ReadPlan(operation=operation, profile=profile, olt_label=label)


# ── legacy-result mapping ────────────────────────────────────────────────────


def unreadable_message(reason: Reason, *, fsp: str | None, detail: str) -> str:
    """The legacy-compatible failure text for a typed ``Unreadable``.

    Callers string-match ``olt_busy:`` / ``olt_login_failed:`` / auth wording
    (``olt_ssh_limiter.is_*_message``) and the ``OLT rejected:`` envelope; this
    map is that compatibility surface and is pinned by tests. Raw device output
    never reaches it (the package carries none).
    """
    from app.services.network import olt_ssh_limiter

    port = f" {fsp}" if fsp else ""
    if reason is Reason.OLT_BUSY:
        return olt_ssh_limiter.busy_message(detail or "OLT busy")
    if reason is Reason.LOGIN_REFUSED:
        return olt_ssh_limiter.login_failed_message(detail or "login refused")
    if reason is Reason.AUTH_REJECTED:
        return "Authentication failed"
    if reason is Reason.ROWS_UNPARSED:
        return f"Huawei inventory for{port} had ONT rows that could not be parsed"
    if reason is Reason.TRUNCATED:
        return (
            f"Huawei inventory response for{port} had a table header "
            "but no ONT rows (truncated read)"
        )
    if reason is Reason.UNRECOGNIZED_RESPONSE:
        return f"Huawei inventory response for{port} was not recognized"
    if reason in {
        Reason.UNKNOWN_COMMAND,
        Reason.PARAMETER_ERROR,
        Reason.NEEDS_ARGUMENTS,
        Reason.BOARD_TYPE_ERROR,
        Reason.INSUFFICIENT_PRIVILEGE,
        Reason.CONFIRMATION_REQUESTED,
    }:
        return f"OLT rejected: inventory read{port} ({reason.value})"
    return f"Error: device read{port} unreadable ({reason.value}): {detail}"


def map_inventory(inventory: OntInventory) -> list[RegisteredOntEntry]:
    """``OntInventory`` -> the legacy ``RegisteredOntEntry`` rows Sub persists.

    ``explicitly_empty`` contributes zero rows and the call still succeeds. Rows
    without a serial are dropped exactly as the legacy parser does.
    """
    entries: list[RegisteredOntEntry] = []
    for row in inventory.entries:
        if not row.serial or row.ref.ont_id is None:
            continue
        entries.append(
            RegisteredOntEntry(
                fsp=f"{row.ref.shelf}/{row.ref.slot}/{row.ref.port}",
                onu_id=row.ref.ont_id,
                real_serial=row.serial,
                run_state=row.run_state or "unknown",
            )
        )
    return entries


# ── session lifecycle: exactly-once logout ───────────────────────────────────


def _event_sink(label: str) -> Callable[[str, Mapping[str, str | int]], None]:
    def sink(event: str, fields: Mapping[str, str | int]) -> None:
        # Event names and short fields only (the engine caps output tails).
        logger.debug(
            "device_cli_event olt=%s event=%s fields=%s", label, event, dict(fields)
        )

    return sink


def _logout_on_close() -> bool:
    """Honour ``olt_ssh_logout_on_close`` exactly as the legacy close does."""
    from app.services.network import olt_ssh_limiter

    try:
        return bool(olt_ssh_limiter.get_config().logout_on_close)
    except Exception:
        return True  # unreadable settings keep the safe default (log out)


def _close_package_owned(
    transport: _ClosableTransport, session: CliSession | None
) -> str:
    """End a package-owned session: ONE logout, ONE metric, ONE release.

    ``CliSession.logout`` types nothing into a session that is not provably idle
    at a prompt. Sub's managed ``transport.close`` would run its own ``quit``
    sequence, so the holder is marked closed first and ``close_real`` (plain
    close + slot release with the model grace) does the closing. With
    ``olt_ssh_logout_on_close`` false nothing is typed and ``skipped_dirty`` is
    recorded, as on the legacy path.
    """
    from app.services.network import olt_ssh_metrics

    outcome = "skipped_dirty"
    if session is not None and _logout_on_close():
        try:
            outcome = session.logout().value
        except Exception:
            outcome = "forced"
    olt_ssh_metrics.record_logout(outcome)
    close_real = getattr(transport, "close_real", None)
    holder = getattr(transport, "_olt_holder", None)
    if callable(close_real):
        if isinstance(holder, dict):
            holder["closed"] = True  # Sub's close() becomes a no-op: no second quit
        try:
            close_real()
        except Exception:
            logger.debug("device_cli_close_real_failed", exc_info=True)
    else:
        try:
            transport.close()
        except Exception:
            logger.debug("device_cli_transport_close_failed", exc_info=True)
    return outcome


def _evidence_log(label: str, operation: str, result: Ok[Any] | Unreadable) -> None:
    evidence = result.evidence
    logger.info(
        "device_cli_read olt=%s op=%s result=%s profile=%s digest=%s template=%s duration_ms=%d",
        label,
        operation,
        "ok" if isinstance(result, Ok) else f"unreadable:{result.reason.value}",
        evidence.profile_id,
        evidence.profile_digest[:12],
        evidence.template_id,
        evidence.duration_ms,
    )


def _record(label: str, operation: str, result: str, reason: str = "") -> None:
    from app.services.network import olt_ssh_metrics

    olt_ssh_metrics.record_device_cli_read(label, operation, result, reason)


def _adapter_failure(label: str, operation: str, exc: BaseException) -> str:
    logger.exception("device_cli_adapter_error olt=%s op=%s", label, operation)
    _record(label, operation, "adapter_error")
    return f"Error: device-cli adapter failure ({type(exc).__name__})"


def _open_session(olt: OLTDevice) -> tuple[_ClosableTransport, Channel]:
    """The ONE place the host opens a shell: Sub's own ``_open_shell`` keeps every
    guard (effect gate, slot, credentials, host key, instrumentation)."""
    from app.services.network import olt_ssh as core

    transport, channel, _policy = core._open_shell(olt)
    return transport, channel


# ── R2: registered-ONT inventory ─────────────────────────────────────────────


def read_registered_onts(
    olt: OLTDevice, normalized_fsps: list[str], *, timeout_sec: int
) -> tuple[bool, str, list[RegisteredOntEntry]] | None:
    """Package path for ``get_registered_ont_serials``; ``None`` = use legacy.

    Call only after ``device_cli_gate.selected`` said yes. ``normalized_fsps`` is
    already validated, de-duplicated and non-empty. The whole call, including the
    session priming, is bounded by ``timeout_sec`` and each port by 30 s, as on
    the legacy path.
    """
    plan = plan_read(olt, OP_LIST_ONTS, fsps=normalized_fsps)
    if plan is None:
        return None

    label = plan.olt_label
    started = time.monotonic()
    try:
        transport, channel = _open_session(olt)
    except _OPEN_ERRORS as exc:
        _record(label, OP_LIST_ONTS, "unreadable", Reason.CONNECTION_FAILED.value)
        return False, f"Connection failed: {exc}", []

    session: CliSession | None = None
    try:
        shell = ParamikoShellChannel(channel)
        session = CliSession.attach(shell, plan.profile, events=_event_sink(label))
        prepared = session.prepare()
        if isinstance(prepared, Unreadable):
            _evidence_log(label, OP_LIST_ONTS, prepared)
            _record(label, OP_LIST_ONTS, "unreadable", prepared.reason.value)
            return (
                False,
                unreadable_message(prepared.reason, fsp=None, detail=prepared.detail),
                [],
            )
        dialect = dialect_for(plan.profile.dialect)
        if dialect is None:  # unreachable: plan_read composed with it
            raise RuntimeError("dialect vanished after planning")
        entries: list[RegisteredOntEntry] = []
        for fsp in normalized_fsps:
            remaining = timeout_sec - (time.monotonic() - started)
            if remaining <= 0:
                _record(label, OP_LIST_ONTS, "unreadable", Reason.PROMPT_TIMEOUT.value)
                return (
                    False,
                    f"Error: Huawei inventory exceeded {timeout_sec}s before port {fsp}",
                    [],
                )
            # Same bound as legacy: min(30, remaining), so the total stays within
            # timeout_sec. An overrun reads as EOF and fails the call.
            shell.deadline = time.monotonic() + max(
                1, min(_PER_PORT_SECONDS, int(remaining))
            )
            result = list_onts(session, dialect.parse_ref(fsp))
            if isinstance(result, Unsupported):
                _record(label, OP_LIST_ONTS, "unsupported")
                return (
                    False,
                    f"Error: device read unsupported on profile {result.profile_id}",
                    [],
                )
            if isinstance(result, Unreadable):
                _evidence_log(label, OP_LIST_ONTS, result)
                _record(label, OP_LIST_ONTS, "unreadable", result.reason.value)
                return (
                    False,
                    unreadable_message(result.reason, fsp=fsp, detail=result.detail),
                    [],
                )
            assert isinstance(result, Ok) and isinstance(result.value, OntInventory)
            _evidence_log(label, OP_LIST_ONTS, result)
            entries.extend(map_inventory(result.value))
        _record(label, OP_LIST_ONTS, "ok")
        return (
            True,
            f"Found {len(entries)} registered ONTs on {len(normalized_fsps)} ports",
            entries,
        )
    except Exception as exc:
        if isinstance(exc, _SESSION_CONNECTION_ERRORS):
            _record(label, OP_LIST_ONTS, "unreadable", Reason.CONNECTION_FAILED.value)
            logger.error(
                "Error getting registered ONT serials from OLT %s: %s", label, exc
            )
            return False, f"Error: {exc}", []
        # RuntimeError / ValueError after the open are adapter bugs, not
        # connectivity: counted as adapter_error.
        return False, _adapter_failure(label, OP_LIST_ONTS, exc), []
    finally:
        _close_package_owned(transport, session)


__all__ = [
    "OP_LIST_ONTS",
    "ParamikoShellChannel",
    "PgAdvisoryLockStore",
    "ReadPlan",
    "acquire_device_slot",
    "device_key_for",
    "map_inventory",
    "paging_mismatch",
    "pg_store_factory",
    "plan_read",
    "read_registered_onts",
    "resolve_profile",
    "ssh_policy_mismatch",
    "unreadable_message",
]
