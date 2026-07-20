from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from cryptography.fernet import Fernet

from app.models.catalog import (
    NasDevice,
    NasDeviceStatus,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import OntAssignment, OntUnit
from app.models.radius import RadiusClient, RadiusServer
from app.models.radius_active_session import RadiusActiveSession
from app.services import credential_key_rotation as key_rotation
from app.services import credential_lifecycle_cleanup as cleanup
from app.services.credential_crypto import decrypt_credential_with_key


def _configure_key(monkeypatch):
    key = Fernet.generate_key()
    for module in (key_rotation, cleanup):
        monkeypatch.setattr(module, "get_encryption_key", lambda: key)
        monkeypatch.setattr(module, "get_previous_encryption_key", lambda: None)
    monkeypatch.setattr(
        cleanup, "publish_credential_integrity_snapshot", lambda *a, **k: True
    )
    monkeypatch.setattr(
        cleanup, "external_radius_nas_client_ips", lambda _db, _ips: set()
    )
    monkeypatch.setattr(
        cleanup,
        "external_radius_nas_secret_inventory",
        lambda _db, _ips: SimpleNamespace(
            recoverable_secrets={},
            conflicting_client_ips=frozenset(),
            present_client_ips=frozenset(),
        ),
    )
    return key


def test_cleanup_plan_harmonizes_inactive_nas_lifecycle(db_session, monkeypatch):
    _configure_key(monkeypatch)
    device = NasDevice(
        name="retired-router",
        is_active=False,
        status=NasDeviceStatus.active,
        nas_ip="10.20.30.40",
        shared_secret="enc:lost-key-ciphertext",
    )
    db_session.add(device)
    db_session.commit()

    plan = cleanup.build_credential_lifecycle_cleanup_plan(db_session)

    assert plan.blocked == 0
    assert plan.eligible == 1
    assert plan.action_counts == {"decommission_nas": 1}
    assert plan.items[0].normalize_nas_status is True
    assert len(plan.digest) == 64


def test_cleanup_plan_blocks_nas_with_nonterminal_subscription(
    db_session, monkeypatch, subscriber, catalog_offer
):
    _configure_key(monkeypatch)
    device = NasDevice(
        name="still-referenced-router",
        is_active=False,
        status=NasDeviceStatus.active,
        shared_secret="enc:lost-key-ciphertext",
    )
    db_session.add(device)
    db_session.flush()
    db_session.add(
        Subscription(
            subscriber_id=subscriber.id,
            offer_id=catalog_offer.id,
            provisioning_nas_device_id=device.id,
            status=SubscriptionStatus.suspended,
        )
    )
    db_session.commit()

    result = cleanup.cleanup_unrecoverable_credentials(db_session)

    assert result.status == "blocked"
    assert result.plan.action_counts == {"blocked_nas_subscription": 1}
    db_session.refresh(device)
    assert device.shared_secret == "enc:lost-key-ciphertext"


def test_cleanup_recovers_referenced_nas_from_authoritative_radius(
    db_session, monkeypatch, subscriber, catalog_offer
):
    key = _configure_key(monkeypatch)
    device = NasDevice(
        name="referenced-router",
        is_active=False,
        status=NasDeviceStatus.decommissioned,
        nas_ip="10.20.30.45",
        shared_secret="enc:lost-key-ciphertext",
    )
    db_session.add(device)
    db_session.flush()
    db_session.add(
        Subscription(
            subscriber_id=subscriber.id,
            offer_id=catalog_offer.id,
            provisioning_nas_device_id=device.id,
            status=SubscriptionStatus.active,
        )
    )
    db_session.commit()
    secret_inventory = SimpleNamespace(
        recoverable_secrets={"10.20.30.45": "authoritative-radius-secret"},
        conflicting_client_ips=frozenset(),
        present_client_ips=frozenset({"10.20.30.45"}),
    )
    monkeypatch.setattr(
        cleanup,
        "external_radius_nas_secret_inventory",
        lambda _db, _ips: secret_inventory,
    )
    monkeypatch.setattr(
        cleanup,
        "external_radius_nas_client_ips",
        lambda _db, _ips: {"10.20.30.45"},
    )
    monkeypatch.setattr(
        cleanup, "remove_external_radius_nas_clients", lambda _db, _ips: 0
    )
    monkeypatch.setattr(cleanup, "stage_audit_event", lambda *a, **k: None)

    plan = cleanup.build_credential_lifecycle_cleanup_plan(db_session)
    result = cleanup.cleanup_unrecoverable_credentials(
        db_session,
        execute=True,
        confirm_plan_digest=plan.digest,
    )

    db_session.refresh(device)
    assert plan.action_counts == {"recover_nas_secret": 1}
    assert plan.items[0].requires_lifecycle_review is True
    assert result.status == "completed"
    assert decrypt_credential_with_key(device.shared_secret, key) == (
        "authoritative-radius-secret"
    )
    assert device.is_active is False
    assert device.status == NasDeviceStatus.decommissioned


def test_cleanup_plan_blocks_nas_with_live_session(db_session, monkeypatch):
    _configure_key(monkeypatch)
    device = NasDevice(
        name="session-router",
        is_active=False,
        status=NasDeviceStatus.decommissioned,
        nas_ip="10.20.30.46",
        shared_secret="enc:lost-key-ciphertext",
    )
    db_session.add(device)
    db_session.flush()
    db_session.add(
        RadiusActiveSession(
            nas_ip_address="10.20.30.46",
            username="active-user",
            acct_session_id="active-session",
            session_start=datetime.now(UTC),
        )
    )
    db_session.commit()

    result = cleanup.cleanup_unrecoverable_credentials(db_session)

    assert result.status == "blocked"
    assert result.plan.action_counts == {"blocked_nas_live_session": 1}


def test_cleanup_execute_requires_matching_reviewed_digest(db_session, monkeypatch):
    _configure_key(monkeypatch)
    device = NasDevice(
        name="digest-router",
        is_active=False,
        status=NasDeviceStatus.decommissioned,
        shared_secret="enc:lost-key-ciphertext",
    )
    db_session.add(device)
    db_session.commit()

    result = cleanup.cleanup_unrecoverable_credentials(
        db_session,
        execute=True,
        confirm_plan_digest="wrong",
    )

    assert result.status == "confirmation_required"
    db_session.refresh(device)
    assert device.shared_secret == "enc:lost-key-ciphertext"


def test_cleanup_execute_clears_owned_fields_and_audits(db_session, monkeypatch):
    _configure_key(monkeypatch)
    device = NasDevice(
        name="cleanup-router",
        is_active=False,
        status=NasDeviceStatus.active,
        nas_ip="10.20.30.50",
        shared_secret="enc:lost-key-ciphertext",
    )
    server = RadiusServer(name="radius", host="radius.local")
    ont = OntUnit(
        serial_number="LOST-WIFI-PASSWORD",
        is_active=True,
        desired_config={
            "wifi": {"password": "enc:lost-key-ciphertext", "ssid": "Dotmac"},
            "wan": {"mode": "routing"},
        },
    )
    db_session.add_all([device, server, ont])
    db_session.flush()
    client = RadiusClient(
        server_id=server.id,
        nas_device_id=device.id,
        client_ip="10.20.30.50",
        shared_secret_hash="hash",
        is_active=True,
    )
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db_session.add_all([client, assignment])
    db_session.commit()

    monkeypatch.setattr(
        cleanup,
        "external_radius_nas_client_ips",
        lambda _db, _ips: {"10.20.30.50"},
    )
    removed: list[set[str]] = []
    monkeypatch.setattr(
        cleanup,
        "remove_external_radius_nas_clients",
        lambda _db, ips: removed.append(ips) or 1,
    )
    audits: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        cleanup,
        "stage_audit_event",
        lambda _db, *, entity_type, entity_id, metadata, **_kwargs: audits.append(
            (entity_type, entity_id, metadata)
        ),
    )

    plan = cleanup.build_credential_lifecycle_cleanup_plan(db_session)
    result = cleanup.cleanup_unrecoverable_credentials(
        db_session,
        execute=True,
        confirm_plan_digest=plan.digest,
    )

    db_session.refresh(device)
    db_session.refresh(client)
    db_session.refresh(ont)
    assert result.status == "completed"
    assert result.local_values_cleared == 2
    assert result.nas_statuses_normalized == 1
    assert result.internal_radius_clients_deactivated == 1
    assert result.external_radius_clients_removed == 1
    assert device.shared_secret is None
    assert device.status == NasDeviceStatus.decommissioned
    assert client.is_active is False
    assert ont.desired_config == {
        "wifi": {"ssid": "Dotmac"},
        "wan": {"mode": "routing"},
    }
    assert removed == [{"10.20.30.50"}]
    assert {entry[0] for entry in audits} == {"NasDevice", "OntUnit"}
    assert (
        next(entry for entry in audits if entry[0] == "OntUnit")[2]["reset_required"]
        is True
    )


def test_cleanup_output_contains_no_record_identity(db_session, monkeypatch):
    _configure_key(monkeypatch)
    device = NasDevice(
        name="private-router-name",
        is_active=False,
        status=NasDeviceStatus.decommissioned,
        nas_ip="10.20.30.60",
        shared_secret="enc:lost-key-ciphertext",
    )
    db_session.add(device)
    db_session.commit()

    payload = cleanup.cleanup_unrecoverable_credentials(db_session).as_dict()
    serialized = str(payload)

    assert str(device.id) not in serialized
    assert device.name not in serialized
    assert device.nas_ip not in serialized
