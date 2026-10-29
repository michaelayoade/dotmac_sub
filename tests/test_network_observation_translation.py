"""Observation translation + disconnect step shaping."""

from __future__ import annotations

import pytest

from app.services.integrations.network_observation_translation import (
    AccountingRecord,
    AuthenticationRecord,
    MalformedObservation,
    UnresolvedObservation,
    build_disconnect_step,
    command_id_for_disconnect,
    translate_observation,
)

KNOWN = {"jane@radius": "sub:subscription:9f2c"}.get

AUTH = {
    "kind": "authentication",
    "subject_ref": "jane@radius",
    "nas_ref": "nas:jabi-bras-1",
    "session_ref": None,
    "outcome": "rejected",
    "event_kind": None,
    "reason_code": "Simultaneous-Use exceeded",
    "input_octets": None,
    "output_octets": None,
    "session_seconds": None,
    "source_ref": "radpostauth:991",
    "observed_at": "2026-10-27T08:15:00Z",
    "fingerprint": "a" * 64,
}

ACCT = {
    "kind": "accounting",
    "subject_ref": "jane@radius",
    "nas_ref": "nas:jabi-bras-1",
    "session_ref": "0000002F",
    "outcome": None,
    "event_kind": "stop",
    "reason_code": "User-Request",
    "input_octets": 1048576,
    "output_octets": 2097152,
    "session_seconds": 3600,
    "source_ref": "radacct:551",
    "observed_at": "2026-10-27T09:15:00Z",
    "fingerprint": "b" * 64,
}


def test_authentication_resolves_to_subject() -> None:
    out = translate_observation(AUTH, resolve_login=KNOWN)
    assert isinstance(out, AuthenticationRecord)
    assert out.subject_ref == "sub:subscription:9f2c"
    assert out.outcome == "rejected" and out.fingerprint == "a" * 64


def test_accounting_carries_counters() -> None:
    out = translate_observation(ACCT, resolve_login=KNOWN)
    assert isinstance(out, AccountingRecord)
    assert out.input_octets == 1048576 and out.session_seconds == 3600


def test_unknown_login_quarantines_with_fingerprint() -> None:
    out = translate_observation(
        dict(AUTH, subject_ref="ghost@radius"), resolve_login=KNOWN
    )
    assert isinstance(out, UnresolvedObservation)
    assert out.username == "ghost@radius" and out.fingerprint == "a" * 64


def test_missing_counter_is_malformed_not_zero() -> None:
    with pytest.raises(MalformedObservation, match="input_octets"):
        translate_observation(dict(ACCT, input_octets=None), resolve_login=KNOWN)


def test_disconnect_step_and_replayable_command() -> None:
    step = build_disconnect_step(
        subject_ref="sub:subscription:9f2c",
        username="jane@radius",
        nas_ref="nas:jabi-bras-1",
        nas_address="10.20.0.14",
        reason_code="subscription_suspended",
        session_ref="0000002F",
    )
    assert step["coa_port"] == 3799 and step["nas_address"] == "10.20.0.14"
    assert (
        command_id_for_disconnect(lifecycle_command_ref="cmd-77")
        == "session-disconnect:cmd-77"
    )
