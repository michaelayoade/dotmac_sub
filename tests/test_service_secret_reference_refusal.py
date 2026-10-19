"""H1 residual paths: a stored/typed secret reference is never resolved or pushed.

A planted fake env var stands in for an app secret; no real secret is used.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.schemas.catalog import AccessCredentialCreate, AccessCredentialUpdate
from app.services import auth_flow
from app.services import catalog as catalog_service

PLANTED_VAR = "H1_RESIDUAL_FAKE_SECRET"
PLANTED_VALUE = "planted-fake-value-not-a-secret"
REFS = [
    f"env://{PLANTED_VAR}",
    f"  ENV://{PLANTED_VAR}  ",
    "bao://secret/x#y",
    "OpenBao://secret/x#y",
    "vault://secret/x#y",
]
WRAPPERS = ["enc:abc", "PLAIN:abc", " cleartext:abc"]


@pytest.fixture(autouse=True)
def _planted(monkeypatch):
    monkeypatch.setenv(PLANTED_VAR, PLANTED_VALUE)


# R-a: bulk activate / every service-password write -> hash_service_secret
@pytest.mark.parametrize("value", REFS + WRAPPERS)
def test_hash_service_secret_refuses_references_and_wrappers(value):
    with pytest.raises(ValueError) as exc:
        auth_flow.hash_service_secret(value)
    assert PLANTED_VAR not in str(exc.value)
    assert PLANTED_VALUE not in str(exc.value)


def test_hash_service_secret_still_accepts_ordinary_password():
    stored = auth_flow.hash_service_secret("Ordinary-Pass-123")
    assert stored and stored.startswith(("enc:", "plain:"))


def test_bulk_activate_upsert_refuses_reference_without_writing():
    from app.services import web_provisioning_bulk_activate as bulk

    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    subscriber = SimpleNamespace(id=uuid.uuid4())
    with pytest.raises(ValueError):
        bulk._upsert_access_credential(
            db,
            subscriber=subscriber,
            username="u",
            password=f"env://{PLANTED_VAR}",
        )
    db.add.assert_not_called()


# R-a: admin API create/update of AccessCredential.secret_hash
class TestAccessCredentialApiRefusal:
    @pytest.mark.parametrize("value", REFS + WRAPPERS)
    @patch("app.services.catalog.credentials._sync_credential_to_radius")
    def test_create_refuses(self, mock_sync, value, db_session, subscriber):
        with pytest.raises(HTTPException) as exc:
            catalog_service.access_credentials.create(
                db_session,
                AccessCredentialCreate(
                    subscriber_id=subscriber.id,
                    username=f"u-{uuid.uuid4().hex[:8]}",
                    secret_hash=value,
                ),
            )
        assert exc.value.status_code == 422
        assert PLANTED_VAR not in str(exc.value.detail)
        mock_sync.assert_not_called()

    @pytest.mark.parametrize("value", REFS + WRAPPERS)
    @patch("app.services.catalog.credentials._sync_credential_to_radius")
    def test_update_refuses(self, mock_sync, value, db_session, subscriber):
        cred = catalog_service.access_credentials.create(
            db_session,
            AccessCredentialCreate(
                subscriber_id=subscriber.id,
                username=f"u-{uuid.uuid4().hex[:8]}",
                secret_hash="Ordinary-Pass-123",
            ),
        )
        before = cred.secret_hash
        with pytest.raises(HTTPException) as exc:
            catalog_service.access_credentials.update(
                db_session, str(cred.id), AccessCredentialUpdate(secret_hash=value)
            )
        assert exc.value.status_code == 422
        db_session.refresh(cred)
        assert cred.secret_hash == before


# R-b: sales-order copy into ONT intent
def test_sales_order_intent_refuses_stored_reference(monkeypatch):
    from app.models.catalog import ConnectionType
    from app.services import sales_orders

    credential = SimpleNamespace(
        username="u", secret_hash=f"env://{PLANTED_VAR}", is_active=True
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = credential
    monkeypatch.setattr(
        "app.services.connection_type_provisioning.resolve_connection_type",
        lambda *a, **k: ConnectionType.pppoe,
    )
    monkeypatch.setattr("app.services.ipv6_pd.pd_enabled", lambda *a, **k: False)
    subscription = SimpleNamespace(
        id=uuid.uuid4(),
        subscriber_id=uuid.uuid4(),
        radius_profile_id=None,
        provisioning_nas_device=None,
    )
    with pytest.raises(HTTPException) as exc:
        sales_orders._build_staged_device_intent(
            db,
            sales_order=SimpleNamespace(),
            line=SimpleNamespace(),
            subscription=subscription,
        )
    assert exc.value.status_code == 409
    assert PLANTED_VAR not in str(exc.value.detail)


# R-b: ONT decrypt helper and OLT command generator never resolve a reference
@pytest.mark.parametrize("value", REFS)
def test_ont_decrypt_helper_never_resolves_reference(value):
    from app.services.network.ont_provision_steps import _decrypt_optional_secret

    assert _decrypt_optional_secret(value) is None


def test_ont_decrypt_helper_still_decrypts_ordinary_value():
    from app.services.network.ont_provision_steps import _decrypt_optional_secret

    stored = auth_flow.hash_service_secret("Ordinary-Pass-123")
    assert _decrypt_optional_secret(stored) == "Ordinary-Pass-123"


def test_ont_decrypt_helper_refuses_decrypted_reference(monkeypatch):
    from app.services.network import ont_provision_steps

    monkeypatch.setattr(
        "app.services.credential_crypto.decrypt_credential",
        lambda v: f"env://{PLANTED_VAR}",
    )
    assert ont_provision_steps._decrypt_optional_secret("enc:whatever") is None


@pytest.mark.parametrize("value", REFS)
def test_olt_command_gen_never_resolves_reference(value):
    from app.services.network.olt_command_gen import _static_pppoe_password

    assert _static_pppoe_password(value) == ""
    assert PLANTED_VALUE not in _static_pppoe_password(value)
