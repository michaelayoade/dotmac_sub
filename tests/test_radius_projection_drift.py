"""RADIUS writer and auditor share one desired-state comparator."""

from types import SimpleNamespace
from typing import Any, cast

from cryptography.fernet import Fernet

from app.services.radius_population import (
    _projection_fingerprint,
    fingerprint_observed_radius_rows,
)
from app.services.radius_projection_planner import compare_radius_projection


def _desired(**modes: str) -> dict[str, Any]:
    return {
        login: cast(Any, SimpleNamespace(plan=SimpleNamespace(mode=mode)))
        for login, mode in modes.items()
    }


def test_projection_drift_is_bidirectional() -> None:
    drift = compare_radius_projection(
        _desired(active="active", blocked="reject", portal="captive"),
        observed_auth={"active", "blocked", "stale"},
        observed_reject={"stale"},
        observed_captive={"stale"},
    )

    assert drift.missing_auth == {"portal"}
    assert drift.stale_auth == {"stale"}
    assert drift.missing_reject == {"blocked"}
    assert drift.stale_reject == {"stale"}
    assert drift.missing_captive == {"portal"}
    assert drift.stale_captive == {"stale"}
    assert drift.usernames == {"blocked", "portal", "stale"}


def test_projection_drift_is_empty_at_parity() -> None:
    drift = compare_radius_projection(
        _desired(active="active", blocked="reject", portal="captive"),
        observed_auth={"active", "blocked", "portal"},
        observed_reject={"blocked"},
        observed_captive={"portal"},
    )

    assert not drift.usernames


def test_projection_drift_detects_exact_attribute_mismatch() -> None:
    drift = compare_radius_projection(
        _desired(active="active"),
        observed_auth={"active"},
        observed_reject=set(),
        observed_captive=set(),
        desired_fingerprints={"active": "expected-digest"},
        observed_fingerprints={"active": "different-digest"},
    )

    assert drift.attribute_drift == {"active"}
    assert drift.usernames == {"active"}


def test_projection_drift_detects_extra_permissive_auth() -> None:
    drift = compare_radius_projection(
        {},
        observed_auth={"orphan-active-user"},
        observed_reject=set(),
        observed_captive=set(),
    )

    assert drift.stale_auth == {"orphan-active-user"}
    assert drift.usernames == {"orphan-active-user"}


def test_projection_fingerprint_covers_password_and_reply_rows(monkeypatch) -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    expected = _projection_fingerprint(
        radcheck_rows=[
            {
                "username": "active",
                "attribute": "Cleartext-Password",
                "op": ":=",
                "value": "secret-a",
            }
        ],
        radreply_rows=[
            {
                "username": "active",
                "attribute": "Mikrotik-Rate-Limit",
                "op": ":=",
                "value": "50M/50M",
            }
        ],
        radusergroup_rows=[],
    )
    observed = fingerprint_observed_radius_rows(
        radcheck_rows=[
            {
                "username": "active",
                "attribute": "Cleartext-Password",
                "op": ":=",
                "value": "secret-b",
            }
        ],
        radreply_rows=[
            {
                "username": "active",
                "attribute": "Mikrotik-Rate-Limit",
                "op": ":=",
                "value": "50M/50M",
            }
        ],
        radusergroup_rows=[],
    )

    assert observed["active"] != expected


def test_concurrency_cutover_detects_wrong_table_and_missing_checks() -> None:
    drift = compare_radius_projection(
        _desired(active="active", blocked="reject", portal="captive"),
        observed_auth={"active", "blocked", "portal"},
        observed_reject={"blocked"},
        observed_captive={"portal"},
        enforce_simultaneous_use=True,
        observed_simultaneous_use_check={"active", "blocked"},
        observed_simultaneous_use_reply={"active", "portal"},
    )

    assert drift.missing_concurrency_check == {"portal"}
    assert drift.stale_concurrency_check == {"blocked"}
    assert drift.misplaced_concurrency_reply == {"active", "portal"}
    assert drift.usernames == {"active", "blocked", "portal"}


def test_concurrency_drift_is_ignored_before_cutover() -> None:
    drift = compare_radius_projection(
        _desired(active="active"),
        observed_auth={"active"},
        observed_reject=set(),
        observed_captive=set(),
        enforce_simultaneous_use=False,
        observed_simultaneous_use_reply={"active"},
    )

    assert not drift.usernames
