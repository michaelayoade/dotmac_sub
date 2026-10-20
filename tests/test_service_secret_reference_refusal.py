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


# ---------------------------------------------------------------------------
# Review fixes (F1, F2, F5, F6, F7): callers
# ---------------------------------------------------------------------------
from app.services import credential_crypto  # noqa: E402


@pytest.fixture
def spy_resolve(monkeypatch):
    """Fail the test if the secret resolver is ever asked to dereference."""
    calls: list[str] = []

    def _boom(ref):
        calls.append(ref)
        return PLANTED_VALUE

    monkeypatch.setattr("app.services.secrets.resolve_secret", _boom)
    return calls


def test_decrypt_stored_secret_never_resolves_and_fails_closed(spy_resolve):
    for ref in REFS:
        assert credential_crypto.decrypt_stored_secret_or_none(ref) is None
    assert spy_resolve == []
    assert credential_crypto.decrypt_stored_secret_or_none("enc:not-valid") is None


def test_ont_decrypt_failure_does_not_push_ciphertext():
    from app.services.network.ont_provision_steps import _decrypt_optional_secret

    assert _decrypt_optional_secret("enc:not-valid-ciphertext") is None


def _inform_call(monkeypatch, values):
    from app.api import tr069_inform

    onu_type = SimpleNamespace(
        id=uuid.uuid4(), name="t", adapter_name="none", tr069_data_model="x"
    )
    ont = SimpleNamespace(id=uuid.uuid4(), onu_type=onu_type)
    monkeypatch.setattr(
        tr069_inform, "find_unique_active_ont_by_serial", lambda *a: ont
    )
    monkeypatch.setattr(
        tr069_inform, "_build_paths_from_onu_type", lambda *a: {"p": "q"}
    )
    monkeypatch.setattr(
        tr069_inform, "resolve_effective_ont_config", lambda *a, **k: {"values": values}
    )
    return tr069_inform.get_device_config("SERIAL1", db=MagicMock())


def test_tr069_inform_never_sends_a_resolved_reference(monkeypatch, spy_resolve):
    out = _inform_call(
        monkeypatch,
        {
            "wifi_ssid": "s",
            "wifi_password": f"env://{PLANTED_VAR}",
            "wan_mode": "pppoe",
            "pppoe_username": "u",
            "pppoe_password": "bao://secret/x#y",
        },
    )
    assert out["wifi"]["password"] is None
    assert out["wan"]["pppoe_password"] is None
    assert PLANTED_VALUE not in repr(out)
    assert spy_resolve == []


def test_tr069_inform_still_returns_ordinary_password(monkeypatch):
    stored = auth_flow.hash_service_secret("Ordinary-Pass-123")
    out = _inform_call(monkeypatch, {"wifi_ssid": "s", "wifi_password": stored})
    assert out["wifi"]["password"] == "Ordinary-Pass-123"


@pytest.mark.parametrize("value", REFS + WRAPPERS)
def test_customer_wifi_change_refuses_reserved_password(value):
    from app.services.network.ont_service_configuration import (
        CustomerWifiConfigurationChange,
        _customer_wifi_change_updates,
    )

    padded = value if len(value.strip()) >= 8 else value + "xxxxxxxx"
    with pytest.raises(Exception) as exc:
        _customer_wifi_change_updates(
            CustomerWifiConfigurationChange(ssid="home", password=padded)
        )
    assert PLANTED_VAR not in str(exc.value)


def test_customer_wifi_change_accepts_ordinary_password():
    from app.services.network.ont_service_configuration import (
        CustomerWifiConfigurationChange,
        _customer_wifi_change_updates,
    )

    updates, _ = _customer_wifi_change_updates(
        CustomerWifiConfigurationChange(ssid="home", password="Ordinary-Pass-123")
    )
    assert "wifi.password" in updates


@pytest.mark.parametrize("value", REFS)
def test_cpe_wifi_change_refuses_reference(value):
    from app.services.network.ont_service_configuration import (
        CpeWifiConfigurationChange,
        _cpe_wifi_change_updates,
    )

    with pytest.raises(Exception):
        _cpe_wifi_change_updates(CpeWifiConfigurationChange(password=value))


def test_staff_onu_mode_form_refuses_reference(monkeypatch):
    from app.services.network import ont_web_forms

    ont = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(
        ont_web_forms.network_service.ont_units,
        "get_including_inactive",
        lambda **k: ont,
    )
    db = MagicMock()
    result = ont_web_forms.update_onu_mode_from_form(
        db, "ont-1", {"wan_mode": "pppoe", "pppoe_password": f"env://{PLANTED_VAR}"}
    )
    assert result.error and PLANTED_VAR not in result.error
    db.add.assert_not_called()


def test_staff_provision_settings_refuse_reference(monkeypatch):
    from app.services import web_network_onts_provisioning as mod

    monkeypatch.setattr(
        mod.network_service.ont_units,
        "get_including_inactive",
        lambda **k: SimpleNamespace(id=uuid.uuid4()),
    )
    kwargs = dict.fromkeys(
        "onu_mode mgmt_ip_mode mgmt_ip_address mgmt_subnet mgmt_gateway "
        "wan_protocol ip_pool_id static_ip_pool_id static_ip static_subnet "
        "static_gateway static_dns lan_ip lan_subnet dhcp_enabled dhcp_start "
        "dhcp_end wifi_enabled wifi_ssid wifi_password wifi_security_mode "
        "wifi_channel pppoe_username pppoe_password".split()
    )
    for field in ("wifi_password", "pppoe_password"):
        res = mod.save_provision_settings(
            MagicMock(), ont_id="ont-1", **{**kwargs, field: f"env://{PLANTED_VAR}"}
        )
        assert res.status_code == 422
        assert PLANTED_VAR not in repr(res.content)


def test_staff_wifi_feature_refuses_reference(monkeypatch):
    import importlib

    ont_features = importlib.import_module("app.services.network.ont_features")
    monkeypatch.setattr(
        ont_features, "get_ont_or_error", lambda db, i: (SimpleNamespace(), None)
    )
    monkeypatch.setattr(ont_features, "_check_capability", lambda *a: None)
    res = ont_features.OntFeatureService.set_wifi_config(
        MagicMock(), "ont-1", password=f"env://{PLANTED_VAR}"
    )
    assert res.success is False
    assert "not allowed" in res.message
    assert PLANTED_VAR not in res.message


def test_reconcile_resolve_refuses_reference_without_resolving():
    from app.services.network.reconcile.applier import ApplyError, _resolve_or_fail

    called: list[str] = []
    ctx = SimpleNamespace(resolve_secret=lambda r: called.append(r) or "x")
    with pytest.raises(ApplyError):
        _resolve_or_fail(
            ctx, SimpleNamespace(), f"env://{PLANTED_VAR}", refuse_reference=True
        )
    assert called == []
    # CR/ACS credentials keep their existing behaviour.
    assert _resolve_or_fail(ctx, SimpleNamespace(), "bao://a/b#c") == "x"


def test_reconcile_resolve_refuses_reference_after_unwrap(spy_resolve):
    from app.services.network.reconcile.applier import ApplyError, _resolve_or_fail
    from app.services.network.reconcile.secrets import credential_secret_resolver

    ctx = SimpleNamespace(resolve_secret=credential_secret_resolver)
    with pytest.raises(ApplyError) as exc_info:
        _resolve_or_fail(
            ctx,
            SimpleNamespace(),
            f"plain:env://{PLANTED_VAR}",
            refuse_reference=True,
        )
    assert PLANTED_VAR not in str(exc_info.value)
    assert spy_resolve == []


def test_enforcement_access_credential_password_never_resolves_reference(
    monkeypatch, spy_resolve
):
    from app.services.network import provisioning_enforcement as pe

    monkeypatch.setattr(pe, "_effective_field", lambda *a, **k: "user1")
    provider = SimpleNamespace(
        get_by_username=lambda u: SimpleNamespace(secret_hash=f"env://{PLANTED_VAR}")
    )
    ont = SimpleNamespace(serial_number="S1")
    assert pe._resolve_access_credential_password(MagicMock(), provider, ont) == ""
    assert spy_resolve == []


def test_tr069_batch_config_wifi_never_resolves_reference(monkeypatch, spy_resolve):
    from app.services.network import tr069_batch_config as tbc

    monkeypatch.setattr(
        tbc,
        "get_desired_config_value",
        lambda config, section, key: {
            ("wifi", "ssid"): "s",
            ("wifi", "password"): f"env://{PLANTED_VAR}",
        }.get((section, key)),
    )
    monkeypatch.setattr(tbc, "desired_config", lambda ont: {})
    batch = tbc.Tr069ConfigBatch.from_ont_config(
        SimpleNamespace(tr069_data_model="Device"), SimpleNamespace()
    )
    assert PLANTED_VALUE not in repr(vars(batch))
    assert spy_resolve == []


def test_olt_preview_flags_omitted_pppoe_password():
    from app.services.network.olt_command_gen import (
        WanServiceSpec,
        _static_pppoe_password,
    )

    ref = f"env://{PLANTED_VAR}"
    assert _static_pppoe_password(ref) == ""
    assert "pppoe_password_unusable" in WanServiceSpec.__dataclass_fields__


def test_radius_import_skips_references():
    from app.services.radius import _normalize_imported_radius_secret

    for attr in ("Cleartext-Password", "Crypt-Password"):
        assert _normalize_imported_radius_secret(attr, f"env://{PLANTED_VAR}") == (
            None,
            False,
        )


def test_uisp_stage_intent_refuses_reference_password_ref():

    from app.services import uisp_control_plane as ucp

    with pytest.raises(ucp.UispIntentError) as exc:
        ucp.stage_intent(
            MagicMock(),
            target_type=next(iter(ucp.UispIntentTargetType)),
            target_id=uuid.uuid4(),
            desired_state={
                "wifi": {"ssid": "s", "password_ref": f"env://{PLANTED_VAR}"}
            },
        )
    assert PLANTED_VAR not in str(exc.value)


def test_uisp_typed_wifi_password_refuses_reserved():
    from app.services import uisp_control_plane as ucp

    intent = SimpleNamespace(desired_state={})
    with pytest.raises(ucp.UispIntentError) as exc:
        ucp.update_intent_desired(
            MagicMock(), intent, wifi_password=f"env://{PLANTED_VAR}"
        )
    assert PLANTED_VAR not in str(exc.value)


def test_uisp_write_adapter_never_resolves_stored_reference(spy_resolve):
    from app.services import uisp_write_adapter as uwa

    mapping = SimpleNamespace(values=None)
    with pytest.raises(uwa.UispWriteAdapterError):
        uwa._materialize_value("wifi.password_ref", f"env://{PLANTED_VAR}", mapping)
    assert spy_resolve == []
