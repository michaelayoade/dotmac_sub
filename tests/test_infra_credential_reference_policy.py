"""F3: secret references in infrastructure credential columns.

Planted fake variable and mocked OpenBao stand in for real secrets.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from app.models.catalog import NasDevice
from app.models.network import OLTDevice
from app.models.router_management import JumpHost, Router
from app.models.tr069 import Tr069AcsServer
from app.services import credential_crypto
from app.services.credential_crypto import (
    ALLOWED_BAO_REFERENCES,
    CredentialReferenceRefused,
    authorize_reference_writes,
    decrypt_credential,
)

PLANTED_VAR = "F3_FAKE_APP_SECRET"
PLANTED_VALUE = "planted-fake-value-not-a-secret"
OLT_REF = "bao://secret/network/olts/olt-1/ssh#password"
ACS_REF = "bao://secret/network/acs/main#cwmp_password"


@pytest.fixture(autouse=True)
def _planted(monkeypatch):
    monkeypatch.setenv(PLANTED_VAR, PLANTED_VALUE)


# --- read side ---------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "field"),
    [(None, None), ("OLTDevice", "ssh_password"), ("NasDevice", "api_password")],
)
def test_env_reference_from_db_is_never_resolved(model, field):
    with pytest.raises(ValueError) as exc:
        decrypt_credential(f"env://{PLANTED_VAR}", model=model, field=field)
    assert PLANTED_VALUE not in str(exc.value)
    assert PLANTED_VAR not in str(exc.value)


def test_env_reference_without_context_is_never_resolved():
    # Legacy call shape used by ~100 call sites: no model/field.
    with pytest.raises(ValueError):
        decrypt_credential(f"env://{PLANTED_VAR}")


def test_allowlisted_bao_reference_resolves_via_mocked_openbao():
    with patch(
        "app.services.secrets.resolve_openbao_ref", return_value="olt-pass"
    ) as resolve:
        assert (
            decrypt_credential(OLT_REF, model="OLTDevice", field="ssh_password")
            == "olt-pass"
        )
        assert (
            decrypt_credential(ACS_REF, model="Tr069AcsServer", field="cwmp_password")
            == "olt-pass"
        )
    assert resolve.call_count == 2


@pytest.mark.parametrize(
    ("ref", "model", "field"),
    [
        # allowlisted field, path outside its prefix
        ("bao://secret/settings/auth#jwt_secret", "OLTDevice", "ssh_password"),
        ("bao://secret/network/acs/x#v", "OLTDevice", "ssh_password"),
        (
            "bao://secret/network/olts/../../settings/auth#k",
            "OLTDevice",
            "ssh_password",
        ),
        ("bao://secret/network/olts/%2e%2e/x#k", "OLTDevice", "ssh_password"),
        ("bao://secret/network/olts/#k", "OLTDevice", "ssh_password"),
        ("bao://secret/network/olts/a/b?x=1#k", "OLTDevice", "ssh_password"),
        # other aliases never resolve, even under the prefix
        ("openbao://secret/network/olts/a#k", "OLTDevice", "ssh_password"),
        ("vault://secret/network/olts/a#k", "OLTDevice", "ssh_password"),
        # non-allowlisted fields
        (OLT_REF, "OLTDevice", "api_password"),
        (OLT_REF, "NasDevice", "ssh_password"),
        (OLT_REF, "Router", "rest_api_password"),
        (OLT_REF, "JumpHost", "ssh_password"),
        # no context at all
        (OLT_REF, None, None),
    ],
)
def test_non_allowlisted_reference_is_refused_without_calling_openbao(
    ref, model, field, caplog
):
    with (
        patch("app.services.secrets.resolve_openbao_ref") as resolve,
        caplog.at_level(logging.WARNING),
    ):
        with pytest.raises(CredentialReferenceRefused) as exc:
            decrypt_credential(ref, model=model, field=field)
    resolve.assert_not_called()
    assert ref not in str(exc.value)
    assert ref not in caplog.text
    assert "credential.reference_refused" in " ".join(
        str(getattr(r, "event", "")) for r in caplog.records
    )


def test_allowlist_is_declared_in_one_place():
    assert dict(ALLOWED_BAO_REFERENCES) == {
        ("OLTDevice", "ssh_password"): "secret/network/olts/",
        ("Tr069AcsServer", "cwmp_password"): "secret/network/acs/",
    }


# --- write side --------------------------------------------------------


@pytest.mark.parametrize(
    ("factory", "ref"),
    [
        (lambda v: OLTDevice(name="o", ssh_password=v), f"env://{PLANTED_VAR}"),
        (lambda v: OLTDevice(name="o", ssh_password=v), "bao://secret/x#y"),
        (lambda v: OLTDevice(name="o", snmp_ro_community=v), OLT_REF),
        (lambda v: Tr069AcsServer(name="a", cwmp_password=v), "vault://secret/x#y"),
        (lambda v: Tr069AcsServer(name="a", connection_request_password=v), ACS_REF),
        (lambda v: NasDevice(name="n", shared_secret=v), "bao://secret/x#y"),
        (lambda v: NasDevice(name="n", ssh_password=v), "OPENBAO://secret/x#y"),
        (lambda v: Router(name="r", rest_api_password=v), "bao://secret/x#y"),
        (lambda v: JumpHost(name="j", ssh_key=v), "  bao://secret/x#y "),
    ],
)
def test_staff_cannot_write_references_to_infrastructure_columns(factory, ref):
    with pytest.raises(CredentialReferenceRefused):
        factory(ref)


def test_assignment_of_reference_is_refused_even_for_allowlisted_field():
    olt = OLTDevice(name="o")
    with pytest.raises(CredentialReferenceRefused):
        olt.ssh_password = OLT_REF  # allowlisted but actor not authorised


def test_authorised_actor_can_write_allowlisted_reference_only():
    with authorize_reference_writes({"system:secrets:write"}):
        olt = OLTDevice(name="o", ssh_password=OLT_REF)
        assert olt.ssh_password == OLT_REF
        with pytest.raises(CredentialReferenceRefused):
            OLTDevice(name="o", ssh_password="bao://secret/settings/auth#k")
        with pytest.raises(CredentialReferenceRefused):
            OLTDevice(name="o", ssh_password=f"env://{PLANTED_VAR}")
        with pytest.raises(CredentialReferenceRefused):
            NasDevice(name="n", ssh_password=OLT_REF)


def test_actor_without_admin_permission_cannot_open_write_scope():
    with pytest.raises(CredentialReferenceRefused):
        with authorize_reference_writes({"network:tr069:write"}):
            pass


def test_resetting_an_unchanged_stored_reference_is_allowed():
    # Edit forms hand the stored value back; that must not trip the guard.
    with authorize_reference_writes({"*"}):
        olt = OLTDevice(name="o", ssh_password=OLT_REF)
    olt.ssh_password = OLT_REF
    assert olt.ssh_password == OLT_REF


def test_plain_and_encrypted_values_still_write_normally():
    olt = OLTDevice(name="o", ssh_password="plain:hunter2")
    assert olt.ssh_password == "plain:hunter2"


# --- NAS secret consumers don't resolve references ---------------------


def test_non_reference_or_none_refuses_reference_text(caplog):
    assert credential_crypto.non_reference_or_none(f"env://{PLANTED_VAR}") is None
    assert credential_crypto.non_reference_or_none("bao://secret/x#y") is None
    assert credential_crypto.non_reference_or_none("s3cret") == "s3cret"
    assert PLANTED_VALUE not in caplog.text


# --- app config resolution is unaffected -------------------------------


def test_app_config_env_resolution_still_works(monkeypatch):
    from app.services.secrets import resolve_secret

    assert resolve_secret(f"env://{PLANTED_VAR}") == PLANTED_VALUE


def test_encryption_key_env_reference_still_resolves(monkeypatch):
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("F3_KEY_HOLDER", key)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "env://F3_KEY_HOLDER")
    assert credential_crypto.get_encryption_key() == key.encode("ascii")


# --- review follow-ups (H1, M2, L4, L5) ---------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        "bao://secret/network/olts/./x#k",
        "bao://secret/network/olts/a\\..\\b#k",
        "bao://SECRET/network/olts/a#k",
        "bao://secret/network/olts//a#k",
        "bao://secret/network/olts/a\n",
        "bao://secret/network/olts/a/",
        "bao://secret/network/olts/a b#k",
        "bao://secret/network/olts/a#k-1",
        "bao://secret/network/olt/a#k",
        "bao://secret/network/olts#k",
    ],
)
def test_prefix_edge_cases_are_refused(ref):
    with patch("app.services.secrets.resolve_openbao_ref") as resolve:
        with pytest.raises(ValueError):
            decrypt_credential(ref, model="OLTDevice", field="ssh_password")
    resolve.assert_not_called()


def test_editing_a_db_loaded_row(db_session):
    from sqlalchemy import update

    olt = OLTDevice(name="f3-edit", ssh_password="plain:x")
    db_session.add(olt)
    db_session.commit()
    # legacy reference row written by core SQL
    db_session.execute(
        update(OLTDevice).where(OLTDevice.id == olt.id).values(ssh_password=OLT_REF)
    )
    db_session.commit()
    db_session.expire_all()
    loaded = db_session.get(OLTDevice, olt.id)
    loaded.ssh_password = OLT_REF  # unchanged: allowed
    loaded.name = "f3-edit-2"
    db_session.flush()
    with pytest.raises(CredentialReferenceRefused):
        loaded.ssh_password = "bao://secret/network/olts/other/ssh#password"
    with authorize_reference_writes({"system:secrets:write"}):
        loaded.ssh_password = "bao://secret/network/olts/other/ssh#password"
    db_session.rollback()


def test_tr069_auth_refuses_connection_request_reference():
    from app.services import tr069_auth

    with patch("app.services.secrets.resolve_openbao_ref") as resolve:
        got = tr069_auth._decrypt_server_password(
            ACS_REF, None, "connection request", field="connection_request_password"
        )
    assert got is None
    resolve.assert_not_called()
    with patch("app.services.secrets.resolve_openbao_ref", return_value="pw"):
        assert (
            tr069_auth._decrypt_server_password(
                ACS_REF, None, "CWMP", field="cwmp_password"
            )
            == "pw"
        )


def test_reconcile_acs_reference_resolves_but_cr_and_env_do_not():
    from types import SimpleNamespace

    from app.services.network.reconcile.applier import (
        ApplyError,
        _resolve_or_fail,
    )
    from app.services.network.reconcile.secrets import credential_secret_resolver

    ctx = SimpleNamespace(resolve_secret=credential_secret_resolver)
    action = SimpleNamespace(kind="x")
    with patch("app.services.secrets.resolve_openbao_ref", return_value="acs-pw"):
        assert (
            _resolve_or_fail(
                ctx, action, ACS_REF, model="Tr069AcsServer", field="cwmp_password"
            )
            == "acs-pw"
        )
        # connection-request ref: no context -> refused
        with pytest.raises(ApplyError):
            _resolve_or_fail(ctx, action, ACS_REF)
    with pytest.raises(ApplyError) as exc:
        _resolve_or_fail(
            ctx,
            action,
            f"env://{PLANTED_VAR}",
            model="Tr069AcsServer",
            field="cwmp_password",
        )
    assert PLANTED_VALUE not in str(exc.value)
    assert PLANTED_VAR not in str(exc.value)


def test_refused_write_maps_to_422_with_fixed_message():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.errors import register_error_handlers

    app = FastAPI()
    register_error_handlers(app)

    @app.get("/boom")
    def boom():
        OLTDevice(name="o", ssh_password=f"env://{PLANTED_VAR}")

    resp = TestClient(app, raise_server_exceptions=False).get(
        "/boom", headers={"Accept": "application/json"}
    )
    assert resp.status_code == 422
    assert PLANTED_VAR not in resp.text


@pytest.mark.parametrize(
    "literal", ["BAO://secret/network/olts/a#k", " bao://secret/network/olts/a#k"]
)
def test_misspelled_scheme_is_a_literal_and_never_reaches_openbao(literal):
    with patch("app.services.secrets.resolve_openbao_ref") as resolve:
        assert decrypt_credential(literal) == literal
    resolve.assert_not_called()
    with pytest.raises(CredentialReferenceRefused):
        OLTDevice(name="o", ssh_password=literal)
