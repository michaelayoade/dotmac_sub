from __future__ import annotations

import json

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from app.services import secrets
from app.services.kernel_key_provider import KEYRING_REF
from app.services.kernel_secret_source import OPTIONAL_SECRET_REFS, SECRET_REFS
from scripts.setup import verify_openbao_boot_secrets as preflight


def _keyring(*, status: str = "active") -> str:
    return json.dumps(
        [
            {
                "key_id": "deployment",
                "key": Fernet.generate_key().decode(),
                "status": status,
            }
        ]
    )


def test_preflight_accepts_non_empty_required_fields() -> None:
    result = preflight.check_required_boot_secrets(
        {"first": "ref-one", "second": "ref-two"},
        lambda reference: f"value:{reference}",
    )
    assert result.ok
    assert result.checked_names == ("first", "second")
    assert result.failed_names == ()


def test_preflight_reports_only_names_for_empty_and_unavailable_fields() -> None:
    def resolve(reference: str) -> str:
        if reference == "unavailable-ref":
            raise RuntimeError("sensitive-value-must-not-be-reported")
        return "   "

    result = preflight.check_required_boot_secrets(
        {"empty": "empty-ref", "unavailable": "unavailable-ref"}, resolve
    )
    assert not result.ok
    assert result.failed_names == ("empty", "unavailable")
    assert "sensitive-value" not in repr(result)


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        "{}",
        "[1]",
        '[{"key_id":"missing-material"}]',
        "[]",
        _keyring(status="retired"),
        _keyring(status="revoked"),
        _keyring(status="unknown"),
        '[{"key_id":"invalid:id","key":"bad"}]',
        '[{"key_id":"deployment","key":"bad"}]',
    ],
    ids=(
        "invalid-json",
        "wrong-shape",
        "invalid-entry",
        "missing-material",
        "empty-keyring",
        "retired-only",
        "revoked-only",
        "unknown-status",
        "invalid-key-id",
        "invalid-key-material",
    ),
)
def test_preflight_rejects_unusable_keyring(raw: str) -> None:
    result = preflight.check_required_boot_secrets(
        {"settings_encryption_keyring": KEYRING_REF}, lambda _: raw
    )
    assert result.failed_names == ("settings_encryption_keyring",)
    assert result.checks[0].status is preflight.BootSecretStatus.INVALID


@pytest.mark.parametrize("duplicate_id", [False, True])
def test_kernel_rejects_multiple_active_keys_or_duplicate_ids(
    duplicate_id: bool,
) -> None:
    entries = json.loads(_keyring())
    other = json.loads(_keyring())[0]
    if not duplicate_id:
        other["key_id"] = "other"
    entries.append(other)
    result = preflight.check_required_boot_secrets(
        {"settings_encryption_keyring": KEYRING_REF}, lambda _: json.dumps(entries)
    )
    assert not result.ok


def test_active_and_retired_keys_are_usable_without_installing_globals() -> None:
    import dotmac_kernel.settings_crypto as crypto

    entries = json.loads(_keyring()) + json.loads(_keyring(status="retired"))
    entries[1]["key_id"] = "previous"
    globals_before = (crypto._provider, crypto._provided, dict(crypto._FERNET_CACHE))
    result = preflight.check_required_boot_secrets(
        {"settings_encryption_keyring": KEYRING_REF}, lambda _: json.dumps(entries)
    )
    assert result.ok
    assert crypto._provider is globals_before[0]
    assert crypto._provided is globals_before[1]
    assert crypto._FERNET_CACHE == globals_before[2]


def test_missing_optional_path_is_allowed_using_runtime_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_reference: str) -> str:
        raise HTTPException(status_code=404, detail="sensitive-payload")

    monkeypatch.setattr(secrets, "resolve_openbao_ref", missing)
    result = preflight.check_optional_boot_secrets(
        {"anchor": "anchor-ref"}, secrets.resolve_openbao_ref_optional
    )
    assert result.ok
    assert result.absent_names == ("anchor",)


@pytest.mark.parametrize("failure", [403, 500, "network"])
def test_optional_auth_network_and_missing_field_errors_gate(
    monkeypatch: pytest.MonkeyPatch, failure: int | str
) -> None:
    def unavailable(_reference: str) -> str:
        if isinstance(failure, int):
            raise HTTPException(status_code=failure, detail="sensitive-payload")
        raise httpx.ConnectError("sensitive-payload")

    monkeypatch.setattr(secrets, "resolve_openbao_ref", unavailable)
    result = preflight.check_optional_boot_secrets(
        {"anchor": "anchor-ref"}, secrets.resolve_openbao_ref_optional
    )
    assert not result.ok
    assert result.absent_names == ()
    assert result.failed_names == ("anchor",)


def test_empty_optional_field_is_invalid() -> None:
    result = preflight.check_optional_boot_secrets({"anchor": "ref"}, lambda _: " ")
    assert not result.ok
    assert result.checks[0].status is preflight.BootSecretStatus.INVALID


def test_feature_owner_can_require_optional_name() -> None:
    result = preflight.check_optional_boot_secrets(
        {"anchor": "ref"}, lambda _: None, required_names=("anchor",)
    )
    assert result.failed_names == ("anchor",)
    with pytest.raises(ValueError, match="boot registry"):
        preflight.check_optional_boot_secrets({}, required_names=("unknown",))


def test_cli_redacts_values_parser_diagnostics_and_exception_bodies(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    material = "sensitive-payload-must-never-be-reported"

    def required(reference: str) -> str:
        if reference == KEYRING_REF:
            return json.dumps([{"key_id": material, "key": material}])
        raise RuntimeError(material)

    result = preflight.check_boot_secrets(
        required_resolver=required, optional_resolver=lambda _: None
    )
    monkeypatch.setattr(preflight, "check_boot_secrets", lambda: result)
    assert preflight.main(["--component", "celery-worker"]) == 1
    captured = capsys.readouterr()
    assert material not in captured.out + captured.err
    assert "celery-worker/settings_encryption_keyring: invalid" in captured.out


def test_registry_is_derived_from_runtime_owners() -> None:
    assert preflight.OPTIONAL_REFS is OPTIONAL_SECRET_REFS
    assert preflight.REQUIRED_REFS == {
        **SECRET_REFS,
        "settings_encryption_keyring": KEYRING_REF,
    }


@pytest.mark.parametrize(
    "name",
    [
        "credential_encryption_key",
        "totp_encryption_key",
        "wireguard_key_encryption_key",
    ],
)
@pytest.mark.parametrize("valid", [False, True])
def test_credential_owners_require_fernet_material(name: str, valid: bool) -> None:
    value = Fernet.generate_key().decode() if valid else "unusable-material"
    result = preflight.check_required_boot_secrets(
        {name: SECRET_REFS[name]}, lambda _: value
    )
    assert result.ok is valid


def test_jwt_and_radius_keep_owner_nonempty_criteria() -> None:
    result = preflight.check_required_boot_secrets(
        {
            name: SECRET_REFS[name]
            for name in ("jwt_secret", "radius_auth_shared_secret")
        },
        lambda _: "owner-accepts-nonempty-material",
    )
    assert result.ok


def test_standalone_cli_suppresses_resolver_logs_and_restores_logging(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    payload = "synthetic-sensitive-resolver-payload"
    previous = logging.root.manager.disable

    def resolver(_reference: str) -> str:
        logging.getLogger("app.services.secrets").error(
            "lookup failed at synthetic-private-reference: %s", payload
        )
        raise RuntimeError(payload)

    def check() -> preflight.BootSecretPreflightResult:
        return preflight.check_required_boot_secrets(
            {"jwt_secret": "unit-ref"}, resolver
        )

    monkeypatch.setattr(preflight, "check_boot_secrets", check)
    with caplog.at_level(logging.ERROR):
        assert preflight.main(["--component", "app"]) == 1
    output = capsys.readouterr()
    assert payload not in output.out + output.err + caplog.text
    assert "synthetic-private-reference" not in output.out + output.err + caplog.text
    assert "app/jwt_secret: unavailable" in output.out
    assert logging.root.manager.disable == previous
