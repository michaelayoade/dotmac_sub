from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet

from app.models.catalog import NasDevice
from app.services import credential_key_rotation as key_rotation
from app.services import credential_rotation_schedule as rotation_schedule
from app.services.credential_crypto import encrypt_credential_with_key
from app.services.credential_key_rotation import CredentialIntegrityResult
from app.services.observability import StateObservation


def _integrity_result(
    *,
    plaintext: int = 0,
    undecryptable: int = 0,
) -> CredentialIntegrityResult:
    counts = {
        "NasDevice.shared_secret": {
            "encrypted": 1,
            "plaintext": plaintext,
            "undecryptable": undecryptable,
            "reference": 0,
            "empty": 0,
        }
    }
    return CredentialIntegrityResult(
        counts=counts,
        totals={
            "encrypted": 1,
            "plaintext": plaintext,
            "undecryptable": undecryptable,
            "reference": 0,
            "empty": 0,
        },
        scanned_at=datetime(2026, 7, 12, tzinfo=UTC),
    )


def test_integrity_scan_classifies_without_exposing_values(db_session, monkeypatch):
    key = Fernet.generate_key()
    row = NasDevice(
        name="integrity-test",
        shared_secret=encrypt_credential_with_key("radius-secret", key),
        ssh_password="plain:ssh-secret",
        api_password="enc:not-valid",
        snmp_community="bao://secret/network#community",
    )
    db_session.add(row)
    db_session.commit()
    monkeypatch.setattr(key_rotation, "get_encryption_key", lambda: key)
    monkeypatch.setattr(key_rotation, "get_previous_encryption_key", lambda: None)

    result = key_rotation.scan_credential_encryption_integrity(db_session)

    assert result.counts["NasDevice.shared_secret"]["encrypted"] == 1
    assert result.counts["NasDevice.ssh_password"]["plaintext"] == 1
    assert result.counts["NasDevice.api_password"]["undecryptable"] == 1
    assert result.counts["NasDevice.snmp_community"]["reference"] == 1
    serialized = json.dumps(
        {"counts": result.counts, "observations": result.observations()}
    )
    assert str(row.id) not in serialized
    assert "radius-secret" not in serialized
    assert "ssh-secret" not in serialized


def test_integrity_scan_reads_ont_credentials_through_network_owner(
    db_session, monkeypatch
):
    monkeypatch.setattr(key_rotation, "get_encryption_key", lambda: None)
    monkeypatch.setattr(key_rotation, "get_previous_encryption_key", lambda: None)
    monkeypatch.setattr(
        key_rotation,
        "desired_config_values_for_paths",
        lambda _db, paths: [(paths[0], "plain:wifi-secret")],
    )

    result = key_rotation.scan_credential_encryption_integrity(db_session)

    scope = "OntUnit.desired_config.wifi.password"
    assert result.counts[scope]["plaintext"] == 1


def test_shared_observability_snapshot_round_trip(monkeypatch):
    from app.services import app_cache, observability

    stored: dict[str, object] = {}

    def fake_set(key, value, ttl):
        stored.update({"key": key, "value": value, "ttl": ttl})
        return True

    monkeypatch.setattr(app_cache, "set_json", fake_set)
    monkeypatch.setattr(app_cache, "get_json", lambda key: stored.get("value"))

    assert observability.publish_state_snapshot(
        "credentials",
        [
            StateObservation(
                signal="undecryptable",
                scope="NasDevice.shared_secret",
                value=2,
            )
        ],
        status="error",
        now=datetime(2026, 7, 12, tzinfo=UTC),
    )
    loaded = observability.load_state_snapshot("credentials")

    assert loaded is not None
    assert loaded["status"] == "error"
    assert loaded["observations"] == [
        {
            "signal": "undecryptable",
            "scope": "NasDevice.shared_secret",
            "value": 2.0,
        }
    ]
    assert int(stored["ttl"]) == 7 * 86_400


def test_shared_observability_rejects_unbounded_scope():
    from app.services.observability import publish_state_snapshot

    with pytest.raises(ValueError, match="Invalid observability scope"):
        publish_state_snapshot(
            "credentials",
            [StateObservation(signal="plaintext", scope="record id=123", value=1)],
        )


def test_generic_collector_exports_credential_snapshot(monkeypatch):
    from app import metrics
    from app.services import observability

    snapshot = {
        "domain": "credentials",
        "status": "degraded",
        "observed_at": datetime.now(UTC).isoformat(),
        "observations": [
            {
                "signal": "plaintext",
                "scope": "all",
                "value": 3,
            }
        ],
    }
    monkeypatch.setattr(
        observability,
        "load_state_snapshot",
        lambda domain: snapshot if domain == "credentials" else None,
    )

    families = list(metrics._ObservabilityStateCollector().collect())
    by_name = {family.name: family for family in families}
    samples = by_name["observability_state"].samples

    assert any(
        sample.labels
        == {"domain": "credentials", "signal": "plaintext", "scope": "all"}
        and sample.value == 3
        for sample in samples
    )
    status_samples = by_name["observability_snapshot_status"].samples
    assert any(
        sample.labels == {"domain": "credentials", "status": "degraded"}
        and sample.value == 1
        for sample in status_samples
    )


def test_daily_rotation_blocks_on_undecryptable_inventory(db_session, monkeypatch):
    integrity = _integrity_result(undecryptable=2)

    @contextmanager
    def fake_lock(*_args, **_kwargs):
        yield db_session, True

    monkeypatch.setattr(
        rotation_schedule.db_session_adapter,
        "advisory_lock",
        fake_lock,
    )
    monkeypatch.setattr(
        rotation_schedule,
        "scan_credential_encryption_integrity",
        lambda _db: integrity,
    )
    monkeypatch.setattr(
        rotation_schedule,
        "_managed_key_source",
        lambda _db: (True, "openbao_env_ref"),
    )
    monkeypatch.setattr(
        rotation_schedule,
        "evaluate_scheduled_rotation",
        lambda _db: pytest.fail("rotation must not run with corrupt ciphertext"),
    )
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        rotation_schedule,
        "_publish_integrity_state",
        lambda *_args, **kwargs: published.append(kwargs),
    )

    result = rotation_schedule.run_scheduled_credential_rotation()

    assert result["status"] == "blocked"
    assert result["reason"] == "credential_integrity_failed"
    assert result["integrity_undecryptable"] == 2
    assert published[0]["key_source"] == "openbao_env_ref"


def test_security_task_records_lock_contention_as_skip(monkeypatch):
    from app.tasks import security

    monkeypatch.setattr(
        security,
        "run_rotation",
        lambda: {"status": "already_running", "rotated": False},
    )
    skips: list[tuple[str, str]] = []
    monkeypatch.setattr(
        security,
        "record_task_skip",
        lambda task, *, reason: skips.append((task, reason)) or 1,
    )
    monkeypatch.setattr(
        security,
        "record_task_run",
        lambda *_args, **_kwargs: pytest.fail("skip must not record success"),
    )

    result = security.run_scheduled_credential_rotation.run()

    assert result["status"] == "already_running"
    assert skips == [(security._TASK_NAME, "already_running")]
