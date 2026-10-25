"""OLT SSH host-key trust on first use (decision D3, phase 1: record only).

* ``observe`` turns the key paramiko negotiated into a public fingerprint.
* ``evaluate`` is pure: compare it with the recorded key and the mode.
* ``record_observation`` and ``pin_presented_key`` are the ONLY writers of the
  ``olt_ssh_host_key_*`` / ``olt_ssh_presented_key_*`` columns.
  ``record_observation`` runs after the login in its own short out-of-band
  transaction (never on the caller's session, no DB access before auth);
  ``pin_presented_key`` is the explicit admin command on the request session.

Only public-key evidence is handled (type, SHA256 fingerprint). No password,
private key or full key blob is logged or stored.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_RECORD = "record"
MODE_ENFORCE = "enforce"
MODES = (MODE_OFF, MODE_RECORD, MODE_ENFORCE)
DEFAULT_MODE = MODE_RECORD
SETTING_KEY = "olt_ssh_host_key_enforcement"

OUTCOME_OFF = "off"
OUTCOME_FIRST_SEEN = "first_seen"
OUTCOME_MATCH = "match"
OUTCOME_MISMATCH = "mismatch"
OUTCOME_UNKNOWN_REFUSED = "unknown_refused"
OUTCOME_MISMATCH_REFUSED = "mismatch_refused"


class OltHostKeyRefused(RuntimeError):
    """Enforce mode refused the server key before any credential was sent."""


@dataclass(frozen=True)
class HostKeyObservation:
    key_type: str
    sha256: str  # "SHA256:<unpadded base64>", same form as OpenSSH
    base64: str


@dataclass(frozen=True)
class HostKeyVerdict:
    outcome: str
    refuse: bool = False


def observe(transport: Any) -> HostKeyObservation | None:
    """Read the negotiated server host key after key exchange."""
    key = transport.get_remote_server_key()
    if key is None:
        return None
    blob = key.asbytes()
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return HostKeyObservation(
        key_type=str(key.get_name()),
        sha256=f"SHA256:{digest}",
        base64=base64.b64encode(blob).decode(),
    )


_MODE_TTL_SECONDS = 60.0
_mode_cache: tuple[float, str] | None = None
_last_good_mode: str | None = None
_mode_lock = threading.Lock()


def clear_mode_cache() -> None:
    """Test hook: forget the cache AND the last successfully read mode."""
    global _mode_cache, _last_good_mode
    with _mode_lock:
        _mode_cache = None
        _last_good_mode = None


def get_mode() -> str:
    """Current mode (60 s cache).

    If the setting cannot be read (or holds an invalid value) the last mode
    read successfully by this process is used, so an outage can never silently
    downgrade ``enforce``. With no prior value the default is ``record``.
    """
    global _mode_cache, _last_good_mode
    now = time.monotonic()
    cached = _mode_cache
    if cached is not None and now - cached[0] < _MODE_TTL_SECONDS:
        return cached[1]
    mode: str | None = None
    try:
        from app.models.domain_settings import SettingDomain
        from app.services.db_session_adapter import db_session_adapter
        from app.services.settings_spec import resolve_value

        with db_session_adapter.read_session() as db:
            raw = resolve_value(db, SettingDomain.network, SETTING_KEY)
        candidate = str(raw or "").strip().lower()
        if candidate in MODES:
            mode = candidate
    except Exception:
        logger.debug("olt_ssh_host_key_mode_unreadable", exc_info=True)
    with _mode_lock:
        if mode is not None:
            _last_good_mode = mode
        else:
            mode = _last_good_mode or DEFAULT_MODE
            logger.warning("olt_ssh_host_key_mode_unreadable using=%s", mode)
        _mode_cache = (now, mode)
    return mode


def evaluate(
    *,
    mode: str,
    stored_sha256: str | None,
    observed: HostKeyObservation | None,
) -> HostKeyVerdict:
    """Pure decision. Only ``enforce`` ever refuses."""
    if mode == MODE_OFF:
        return HostKeyVerdict(OUTCOME_OFF)
    enforce = mode == MODE_ENFORCE
    if observed is None:
        return (
            HostKeyVerdict(OUTCOME_UNKNOWN_REFUSED, True)
            if enforce
            else HostKeyVerdict(OUTCOME_OFF)
        )
    if not stored_sha256:
        if enforce:
            return HostKeyVerdict(OUTCOME_UNKNOWN_REFUSED, True)
        return HostKeyVerdict(OUTCOME_FIRST_SEEN)
    if stored_sha256 == observed.sha256:
        return HostKeyVerdict(OUTCOME_MATCH)
    if enforce:
        return HostKeyVerdict(OUTCOME_MISMATCH_REFUSED, True)
    return HostKeyVerdict(OUTCOME_MISMATCH)


def _is_mismatch(outcome: str) -> bool:
    return outcome in (OUTCOME_MISMATCH, OUTCOME_MISMATCH_REFUSED)


def log_and_count(
    olt_label: str, stored_sha256: str | None, observed: Any, verdict: HostKeyVerdict
) -> None:
    """Warn + metric on mismatch. Fingerprints are public; nothing secret."""
    if not _is_mismatch(verdict.outcome):
        return
    logger.warning(
        "OLT %s SSH host key MISMATCH: recorded %s, presented %s %s%s",
        olt_label,
        stored_sha256,
        observed.key_type,
        observed.sha256,
        " (connection refused)"
        if verdict.refuse
        else " (record-only, connection allowed)",
    )
    try:
        from app.services.network import olt_ssh_metrics

        olt_ssh_metrics.record_host_key_mismatch(olt_label)
    except Exception:
        logger.debug("olt_ssh_host_key_metric_failed", exc_info=True)


_LOCK_TIMEOUT_MS = 200
_STATEMENT_TIMEOUT_MS = 500


def record_observation(olt_id: Any, observed: HostKeyObservation, *, mode: str) -> None:
    """Persist what a login saw, out of band (ADR 0017 evidence writer).

    One short ``create_session()`` transaction, independent of any caller, with
    a lock and statement timeout. Every step is a single conditional UPDATE, so
    concurrent first sightings cannot overwrite each other:

    1. ``record`` mode, no key stored yet: pin it (``WHERE sha IS NULL``).
    2. Stored key equals the presented one: refresh ``last_seen``.
    3. Otherwise: remember the presented key (what an operator may trust) and,
       when a key IS stored, stamp ``last_mismatch_at``.

    A stored key is never replaced here; only :func:`pin_presented_key` does
    that. Fail-soft: evidence loss must never fail an SSH login.
    """
    if mode == MODE_OFF or olt_id is None:
        return
    session = None
    try:
        from sqlalchemy import text, update

        from app.models.network import OLTDevice
        from app.services.db_session_adapter import db_session_adapter

        now = datetime.now(UTC)
        session = db_session_adapter.create_session()
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT_MS}ms'"))
            session.execute(
                text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT_MS}ms'")
            )
        col = OLTDevice
        done = False
        if mode == MODE_RECORD:
            res = session.execute(
                update(col)
                .where(col.id == olt_id, col.olt_ssh_host_key_sha256.is_(None))
                .values(
                    olt_ssh_host_key_type=observed.key_type,
                    olt_ssh_host_key_sha256=observed.sha256,
                    olt_ssh_host_key_first_seen_at=now,
                    olt_ssh_host_key_last_seen_at=now,
                    olt_ssh_presented_key_type=None,
                    olt_ssh_presented_key_sha256=None,
                )
            )
            done = bool(res.rowcount)
        if not done:
            res = session.execute(
                update(col)
                .where(col.id == olt_id, col.olt_ssh_host_key_sha256 == observed.sha256)
                .values(
                    olt_ssh_host_key_last_seen_at=now,
                    olt_ssh_presented_key_type=None,
                    olt_ssh_presented_key_sha256=None,
                )
            )
            done = bool(res.rowcount)
        if not done:
            session.execute(
                update(col)
                .where(col.id == olt_id)
                .values(
                    olt_ssh_presented_key_type=observed.key_type,
                    olt_ssh_presented_key_sha256=observed.sha256,
                )
            )
            session.execute(
                update(col)
                .where(
                    col.id == olt_id,
                    col.olt_ssh_host_key_sha256.is_not(None),
                    col.olt_ssh_host_key_sha256 != observed.sha256,
                )
                .values(olt_ssh_host_key_last_mismatch_at=now)
            )
        session.commit()
    except Exception:
        logger.warning("olt_ssh_host_key_record_failed olt=%s", olt_id, exc_info=True)
        if session is not None:
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass


class HostKeyTrustError(ValueError):
    """The operator's trust request cannot be applied; message is safe to show."""


def pin_presented_key(*, db: Any, request: Any, olt: Any, expected_sha256: str) -> dict:
    """Pin the exact fingerprint the operator saw (caller's session).

    Refuses unless it still equals the key the OLT last presented. Works with
    or without a stored key (so ``enforce`` cannot lock an OLT out for good).
    The audit event is staged in the same transaction as the key change.
    """
    from app.services.audit_adapter import AuditActor, stage_audit_event
    from app.services.audit_helpers import (
        _resolve_request_actor_id,
        _resolve_request_actor_type,
    )
    from app.services.network.action_logging import actor_label

    expected = (expected_sha256 or "").strip()
    presented = getattr(olt, "olt_ssh_presented_key_sha256", None)
    if not expected or not presented:
        raise HostKeyTrustError(
            "The OLT has not presented a key to trust. Test SSH first, then retry."
        )
    if presented != expected:
        raise HostKeyTrustError(
            "The presented host key changed since you looked. Review the new "
            "fingerprint and confirm again."
        )
    now = datetime.now(UTC)
    before = {
        "previous_sha256": olt.olt_ssh_host_key_sha256,
        "previous_type": olt.olt_ssh_host_key_type,
        "trusted_sha256": presented,
        "trusted_type": olt.olt_ssh_presented_key_type,
    }
    olt.olt_ssh_host_key_type = olt.olt_ssh_presented_key_type
    olt.olt_ssh_host_key_sha256 = presented
    olt.olt_ssh_host_key_first_seen_at = now
    olt.olt_ssh_host_key_last_seen_at = now
    olt.olt_ssh_host_key_last_mismatch_at = None
    olt.olt_ssh_presented_key_type = None
    olt.olt_ssh_presented_key_sha256 = None
    actor_id = _resolve_request_actor_id(request, None)
    stage_audit_event(
        db,
        action="olt_ssh_host_key_trust",
        entity_type="olt",
        entity_id=str(olt.id),
        actor=AuditActor(
            actor_type=_resolve_request_actor_type(request, actor_id),
            actor_id=actor_id,
            label=actor_label(request),
        ),
        metadata=before,
    )
    db.commit()
    return before


def status_view(olt: Any) -> dict[str, Any]:
    sha = getattr(olt, "olt_ssh_host_key_sha256", None)
    mismatch_at = getattr(olt, "olt_ssh_host_key_last_mismatch_at", None)
    seen_at = getattr(olt, "olt_ssh_host_key_last_seen_at", None)
    if not sha:
        state = "not_recorded"
    elif mismatch_at and (seen_at is None or mismatch_at > seen_at):
        state = "mismatch"
    elif mismatch_at:
        state = "mismatch_history"
    else:
        state = "ok"
    return {
        "key_type": getattr(olt, "olt_ssh_host_key_type", None),
        "sha256": sha,
        "first_seen_at": getattr(olt, "olt_ssh_host_key_first_seen_at", None),
        "last_seen_at": seen_at,
        "last_mismatch_at": mismatch_at,
        "presented_type": getattr(olt, "olt_ssh_presented_key_type", None),
        "presented_sha256": getattr(olt, "olt_ssh_presented_key_sha256", None),
        "state": state,
        "mode": get_mode(),
    }
