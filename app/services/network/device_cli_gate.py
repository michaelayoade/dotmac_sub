"""Kill switch and selection gate for ``dotmac-device-cli`` reads.

This module deliberately does NOT import ``dotmac_device_cli`` (nor the host
adapter): the flag, OLT, operation, priority and scope gates must work, and the
legacy path must run, even if the package is missing or fails to import. Callers
ask :func:`selected` first and import ``device_cli_host`` lazily only when it
says yes (an ``ImportError`` there means legacy, plus a metric).

A read is selected iff ``olt_device_cli_reads_enabled`` AND the OLT is active
with a non-empty id that is in ``olt_device_cli_reads_olts`` AND the operation is
in ``olt_device_cli_reads_ops`` AND the call runs at BACKGROUND priority AND no
``olt_ssh_scope`` is active. Unreadable settings mean OFF. Every refusal happens
before any login, so falling back to legacy cannot double-login.

S1 wires ``list_onts`` only (``get_registered_ont_serials``). BACKGROUND only:
post-write verification readers and interactive callers stay legacy until their
own PRs.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from app.models.network import OLTDevice

logger = logging.getLogger(__name__)

OP_LIST_ONTS = "list_onts"
#: The only operations wired in S1. ``version`` is deliberately NOT wired: its
#: production callers are the firmware upgrade pre-check/post-verify.
WIRED_OPERATIONS = frozenset({OP_LIST_ONTS})

SETTING_ENABLED = "olt_device_cli_reads_enabled"
SETTING_OLTS = "olt_device_cli_reads_olts"
SETTING_OPS = "olt_device_cli_reads_ops"
_SETTINGS_TTL_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class DeviceCliReadSettings:
    """Package-read policy. The default is OFF; unreadable settings are OFF."""

    enabled: bool = False
    olts: frozenset[str] = frozenset()
    ops: frozenset[str] = frozenset()


def parse_csv(value: object) -> frozenset[str]:
    """Comma-separated ids/ops, lower-cased, blanks dropped."""
    if value is None:
        return frozenset()
    return frozenset(
        part.strip().lower() for part in str(value).split(",") if part.strip()
    )


_settings_lock = threading.Lock()
_settings_cache: tuple[float, DeviceCliReadSettings] | None = None
_settings_override: DeviceCliReadSettings | None = None


def set_read_settings_override(settings: DeviceCliReadSettings | None) -> None:
    """Pin the settings (tests); ``None`` restores database resolution."""
    global _settings_override, _settings_cache
    with _settings_lock:
        _settings_override = settings
        _settings_cache = None


def _load_read_settings() -> DeviceCliReadSettings:
    from app.models.domain_settings import SettingDomain
    from app.services.db_session_adapter import db_session_adapter
    from app.services.settings_spec import resolve_values_atomic

    keys = [SETTING_ENABLED, SETTING_OLTS, SETTING_OPS]
    with db_session_adapter.read_session() as db:
        values = resolve_values_atomic(db, SettingDomain.network, keys)
    enabled = values.get(SETTING_ENABLED)
    return DeviceCliReadSettings(
        # Only a real boolean True turns the package on; anything else is off.
        enabled=enabled is True,
        olts=parse_csv(values.get(SETTING_OLTS)),
        ops=parse_csv(values.get(SETTING_OPS)),
    )


def get_read_settings() -> DeviceCliReadSettings:
    """Settings with a 60 s in-process cache. Unreadable settings are OFF."""
    global _settings_cache
    if _settings_override is not None:
        return _settings_override
    now = time.monotonic()
    cached = _settings_cache
    if cached is not None and now - cached[0] < _SETTINGS_TTL_SECONDS:
        return cached[1]
    try:
        loaded = _load_read_settings()
    except Exception:
        logger.debug("device_cli_read_settings_unreadable", exc_info=True)
        loaded = DeviceCliReadSettings()
    with _settings_lock:
        _settings_cache = (now, loaded)
    return loaded


def selected(olt: OLTDevice, operation: str) -> bool:
    """True when this call may take the package path. Evaluate ONCE per call.

    Never raises: any failure here means legacy.
    """
    if operation not in WIRED_OPERATIONS:
        return False
    try:
        settings = get_read_settings()
        if not settings.enabled:
            return False
        olt_id = str(getattr(olt, "id", "") or "").lower()
        # Inactive OLTs are always skipped (an OLT that is down must never be
        # logged into by a canary), whatever the allowlist says.
        if not olt_id or getattr(olt, "is_active", None) is not True:
            return False
        if olt_id not in settings.olts or operation not in settings.ops:
            return False

        from app.services.network import olt_ssh_limiter, olt_ssh_metrics

        # BACKGROUND only in S1: post-write verification and interactive
        # callers stay legacy.
        if (
            olt_ssh_limiter.current_priority()
            is not olt_ssh_limiter.OltSshPriority.BACKGROUND
        ):
            return False
        if olt_ssh_limiter.active_scope(olt) is not None:
            # Scope reuse is not proven under the engine: stay legacy.
            olt_ssh_metrics.record_device_cli_event(
                "scope_active_legacy",
                str(getattr(olt, "name", "") or getattr(olt, "id", "")),
                operation,
            )
            return False
    except Exception:
        logger.debug("device_cli_gate_failed", exc_info=True)
        return False
    return True


__all__ = [
    "OP_LIST_ONTS",
    "WIRED_OPERATIONS",
    "DeviceCliReadSettings",
    "get_read_settings",
    "parse_csv",
    "selected",
    "set_read_settings_override",
]
