"""OLT SSH host-key trust on first use (decision D3, phase 1: record only).

* ``observe`` turns the key paramiko negotiated into a public fingerprint.
* ``evaluate`` is pure: compare it with the recorded key and the mode.
* ``record_observation`` / ``retrust`` are the ONLY writers of the
  ``olt_ssh_host_key_*`` columns. Each runs in its own short transaction,
  never inside the SSH critical path and never on the caller's session.

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
_mode_lock = threading.Lock()


def clear_mode_cache() -> None:
    global _mode_cache
    with _mode_lock:
        _mode_cache = None


def get_mode() -> str:
    """Current mode (60 s cache). Unreadable or invalid -> default ``record``."""
    global _mode_cache
    now = time.monotonic()
    cached = _mode_cache
    if cached is not None and now - cached[0] < _MODE_TTL_SECONDS:
        return cached[1]
    mode = DEFAULT_MODE
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


def record_observation(olt_id: Any, observed: HostKeyObservation, *, mode: str) -> None:
    """Persist first-seen / last-seen / last-mismatch in a short transaction.

    Fail-soft: the SSH login has already succeeded (or been refused) and must
    never fail because evidence could not be written. A recorded key is never
    replaced here; changing it needs the audited ``retrust`` action.
    """
    if mode == MODE_OFF or olt_id is None:
        return
    try:
        from app.models.network import OLTDevice
        from app.services.db_session_adapter import db_session_adapter

        now = datetime.now(UTC)
        with db_session_adapter.session() as db:
            olt = db.get(OLTDevice, olt_id)
            if olt is None:
                return
            if not olt.olt_ssh_host_key_sha256:
                if mode != MODE_RECORD:
                    return  # enforce never trusts an unknown key on its own
                olt.olt_ssh_host_key_type = observed.key_type
                olt.olt_ssh_host_key_sha256 = observed.sha256
                olt.olt_ssh_host_key_first_seen_at = now
                olt.olt_ssh_host_key_last_seen_at = now
            elif olt.olt_ssh_host_key_sha256 == observed.sha256:
                olt.olt_ssh_host_key_last_seen_at = now
            else:
                olt.olt_ssh_host_key_last_mismatch_at = now
    except Exception:
        logger.warning("olt_ssh_host_key_record_failed olt=%s", olt_id, exc_info=True)


def retrust(db: Any, olt: Any) -> dict[str, Any]:
    """Clear the recorded key so the next login re-records (caller commits).

    Returns the cleared public evidence for the audit entry.
    """
    before = {
        "key_type": olt.olt_ssh_host_key_type,
        "sha256": olt.olt_ssh_host_key_sha256,
        "last_mismatch_at": (
            olt.olt_ssh_host_key_last_mismatch_at.isoformat()
            if olt.olt_ssh_host_key_last_mismatch_at
            else None
        ),
    }
    olt.olt_ssh_host_key_type = None
    olt.olt_ssh_host_key_sha256 = None
    olt.olt_ssh_host_key_first_seen_at = None
    olt.olt_ssh_host_key_last_seen_at = None
    olt.olt_ssh_host_key_last_mismatch_at = None
    return before


def retrust_with_audit(db: Any, request: Any, olt: Any) -> None:
    """Clear the recorded key, commit, and write the audit entry."""
    from app.services.audit_helpers import log_audit_event

    cleared = retrust(db, olt)
    db.commit()
    log_audit_event(
        db,
        request,
        action="olt_ssh_host_key_retrust",
        entity_type="olt",
        entity_id=str(olt.id),
        actor_id=None,
        metadata={"cleared": cleared},
    )


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
        "state": state,
        "mode": get_mode(),
    }
