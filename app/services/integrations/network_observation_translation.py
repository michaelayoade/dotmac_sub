"""Translate connector observations into owner record commands.

The POLL capability reports what the RADIUS deployment observed, keyed by
login (`subject_ref` on the wire is the RADIUS username — the connector
cannot know Sub's subjects). Composition owns the login -> subject
resolution; this module owns the shape change into the two record
commands the access owner consumes (`record_authentication`,
`record_accounting` in the suite's network-access module). Building the
translation now, against the frozen contract, means the cutover swaps
the sink, not the semantics.

Unresolvable logins are not dropped silently and not guessed: they come
back as `UnresolvedObservation` so the composition can quarantine them
with the fingerprint intact (the same row re-polled later, after the
login is provisioned, resolves then).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

LoginResolver = Callable[[str], str | None]


@dataclass(frozen=True)
class AuthenticationRecord:
    subject_ref: str
    username: str
    nas_ref: str
    outcome: str
    reason_code: str | None
    observed_at: datetime
    source_ref: str
    fingerprint: str


@dataclass(frozen=True)
class AccountingRecord:
    subject_ref: str
    username: str
    nas_ref: str
    session_ref: str
    event_kind: str
    input_octets: int
    output_octets: int
    session_seconds: int
    observed_at: datetime
    source_ref: str
    fingerprint: str


@dataclass(frozen=True)
class UnresolvedObservation:
    username: str
    kind: str
    fingerprint: str
    source_ref: str


ObservationOutcome = AuthenticationRecord | AccountingRecord | UnresolvedObservation


class MalformedObservation(ValueError):
    """The payload does not satisfy the contract's closed shape."""


def _require(payload: Mapping[str, object], key: str) -> object:
    value = payload.get(key)
    if value is None:
        raise MalformedObservation(f"observation is missing {key!r}")
    return value


def _observed_at(payload: Mapping[str, object]) -> datetime:
    raw = _require(payload, "observed_at")
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MalformedObservation("observed_at is not ISO-8601") from exc


def translate_observation(
    payload: Mapping[str, object], *, resolve_login: LoginResolver
) -> ObservationOutcome:
    kind = str(_require(payload, "kind"))
    username = str(_require(payload, "subject_ref"))
    fingerprint = str(_require(payload, "fingerprint"))
    source_ref = str(_require(payload, "source_ref"))
    subject_ref = resolve_login(username)
    if subject_ref is None:
        return UnresolvedObservation(
            username=username, kind=kind, fingerprint=fingerprint, source_ref=source_ref
        )
    nas_ref = str(_require(payload, "nas_ref"))
    observed_at = _observed_at(payload)
    if kind == "authentication":
        return AuthenticationRecord(
            subject_ref=subject_ref,
            username=username,
            nas_ref=nas_ref,
            outcome=str(_require(payload, "outcome")),
            reason_code=(
                str(payload["reason_code"]) if payload.get("reason_code") else None
            ),
            observed_at=observed_at,
            source_ref=source_ref,
            fingerprint=fingerprint,
        )
    if kind == "accounting":
        return AccountingRecord(
            subject_ref=subject_ref,
            username=username,
            nas_ref=nas_ref,
            session_ref=str(_require(payload, "session_ref")),
            event_kind=str(_require(payload, "event_kind")),
            input_octets=int(_require(payload, "input_octets")),  # type: ignore[arg-type]
            output_octets=int(_require(payload, "output_octets")),  # type: ignore[arg-type]
            session_seconds=int(_require(payload, "session_seconds")),  # type: ignore[arg-type]
            observed_at=observed_at,
            source_ref=source_ref,
            fingerprint=fingerprint,
        )
    raise MalformedObservation(f"unknown observation kind {kind!r}")


def build_disconnect_step(
    *,
    subject_ref: str,
    username: str,
    nas_ref: str,
    nas_address: str,
    reason_code: str,
    coa_port: int = 3799,
    session_ref: str | None = None,
    framed_ipv4_address: str | None = None,
) -> dict[str, object]:
    """One `network.session_disconnect.v1` step input.

    Disconnects are lifecycle/governed-command effects (single executor);
    this only shapes the step. `command_id_for_disconnect` makes a repeat
    of the same governed command replay instead of double-sending.
    """
    return {
        "subject_ref": subject_ref,
        "username": username,
        "nas_ref": nas_ref,
        "nas_address": nas_address,
        "coa_port": coa_port,
        "session_ref": session_ref,
        "framed_ipv4_address": framed_ipv4_address,
        "reason_code": reason_code,
    }


def command_id_for_disconnect(*, lifecycle_command_ref: str) -> str:
    return f"session-disconnect:{lifecycle_command_ref}"


__all__ = [
    "AccountingRecord",
    "AuthenticationRecord",
    "MalformedObservation",
    "ObservationOutcome",
    "UnresolvedObservation",
    "build_disconnect_step",
    "command_id_for_disconnect",
    "translate_observation",
]
