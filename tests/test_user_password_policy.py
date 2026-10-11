"""Synthetic parameter and policy tests; no production tuning evidence."""

from unittest.mock import Mock

import bcrypt
import pytest
from argon2 import extract_parameters
from argon2.low_level import Type, hash_secret
from passlib.hash import pbkdf2_sha256, sha512_crypt

from app.services import auth_flow
from app.services import secrets as secret_service
from app.services import user_passwords as owner


@pytest.fixture
def parameters() -> owner.Argon2idParameters:
    return owner.Argon2idParameters(
        time_cost=1, memory_cost_kib=32, parallelism=1, hash_length=16, salt_length=16
    )


def test_explicit_argon2id_hash_roundtrip(parameters: owner.Argon2idParameters) -> None:
    request = owner.Argon2idHashRequest(password="synthetic é", parameters=parameters)
    stored = owner.hash_user_password(request)
    second = owner.hash_user_password(request)
    assert stored != second
    extracted = extract_parameters(stored)
    assert extracted.type is Type.ID
    assert extracted.version == 19
    assert extracted.memory_cost == parameters.memory_cost_kib
    assert extracted.time_cost == parameters.time_cost
    assert extracted.parallelism == parameters.parallelism
    assert extracted.hash_len == parameters.hash_length
    assert extracted.salt_len == parameters.salt_length
    result = auth_flow.verify_user_password_outcome(
        owner.UserPasswordVerification(
            password="synthetic é",
            stored=stored,
            argon2_limits=owner.Argon2idVerificationLimits(parameters, 256),
        )
    )
    assert result.accepted
    assert result.format is owner.PasswordFormat.ARGON2ID
    assert not result.legacy_upgrade_candidate
    assert not auth_flow.verify_user_password(
        "wrong", stored, argon2_limits=owner.Argon2idVerificationLimits(parameters, 256)
    )
    assert "synthetic" not in repr(request)


@pytest.mark.parametrize(
    "prefix", ["plain:", "enc:", "bao://", "openbao://", "vault://", "env://"]
)
def test_refusal_precedes_decryption_resolution_and_compare(
    prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    decrypt = Mock(side_effect=AssertionError("decrypt reached"))
    resolve = Mock(side_effect=AssertionError("resolver reached"))
    compare = Mock(side_effect=AssertionError("compare reached"))
    monkeypatch.setattr(auth_flow, "decrypt_credential", decrypt)
    monkeypatch.setattr(secret_service, "resolve_secret", resolve)
    monkeypatch.setattr(auth_flow.secrets, "compare_digest", compare)
    result = auth_flow.verify_user_password_outcome(
        owner.UserPasswordVerification(
            password="synthetic",
            stored=prefix + "synthetic",
            reversible_policy=owner.ReversiblePasswordPolicy.REFUSE,
        )
    )
    assert result.status is owner.PasswordVerificationStatus.REVERSIBLE_REFUSED
    assert not result.accepted
    assert not result.legacy_upgrade_candidate
    decrypt.assert_not_called()
    resolve.assert_not_called()
    compare.assert_not_called()


def test_compatibility_reversible_never_upgrade_candidate() -> None:
    result = auth_flow.verify_user_password_outcome(
        owner.UserPasswordVerification(password="synthetic", stored="plain:synthetic")
    )
    assert result.accepted
    assert not result.legacy_upgrade_candidate
    assert auth_flow.verify_service_secret("synthetic", "plain:synthetic")


@pytest.mark.parametrize("scheme", ["pbkdf2", "sha512"])
def test_legacy_hash_candidate_only_after_success(scheme: str) -> None:
    hasher = pbkdf2_sha256 if scheme == "pbkdf2" else sha512_crypt
    stored = hasher.using(rounds=5000).hash("synthetic")
    for policy in owner.ReversiblePasswordPolicy:
        good = auth_flow.verify_user_password_outcome(
            owner.UserPasswordVerification("synthetic", stored, policy)
        )
        bad = auth_flow.verify_user_password_outcome(
            owner.UserPasswordVerification("wrong", stored, policy)
        )
        assert good.accepted and good.legacy_upgrade_candidate
        assert not bad.accepted and not bad.legacy_upgrade_candidate
        assert auth_flow.verify_service_secret("synthetic", stored)
    assert auth_flow.hash_password("synthetic").startswith("$pbkdf2-sha256$")


def test_bcrypt_boundary_and_service_refusal() -> None:
    password = "é" * 36
    stored = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
    result = auth_flow.verify_user_password_outcome(
        owner.UserPasswordVerification(password, stored)
    )
    assert result.accepted and result.legacy_upgrade_candidate
    assert not auth_flow.verify_user_password(password + "é", stored)
    assert not auth_flow.verify_service_secret(password, stored)


@pytest.mark.parametrize("kind", [Type.I, Type.D])
def test_other_argon2_types_denied(
    kind: Type, parameters: owner.Argon2idParameters
) -> None:
    stored = hash_secret(b"synthetic", b"synthetic-salt", 1, 32, 1, 16, kind).decode()
    assert not auth_flow.verify_user_password(
        "synthetic",
        stored,
        argon2_limits=owner.Argon2idVerificationLimits(parameters, 256),
    )


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "",
        "$argon2id$",
        "$argon2id$v=20$m=32,t=1,p=1$bad$bad",
        "$2x$",
        "$2b$",
        "$6$",
        "$pbkdf2-sha256$",
        "unmarked",
    ],
)
def test_invalid_material_denies_without_candidate(stored: str | None) -> None:
    result = auth_flow.verify_user_password_outcome(
        owner.UserPasswordVerification("synthetic", stored)
    )
    assert not result.accepted
    assert not result.legacy_upgrade_candidate


def test_argon2_old_version_denied(parameters: owner.Argon2idParameters) -> None:
    stored = hash_secret(
        b"synthetic", b"synthetic-salt", 1, 32, 1, 16, Type.ID, version=16
    ).decode()
    assert not auth_flow.verify_user_password(
        "synthetic",
        stored,
        argon2_limits=owner.Argon2idVerificationLimits(parameters, 256),
    )


def test_service_argon2_acceptance_not_widened(
    parameters: owner.Argon2idParameters,
) -> None:
    stored = owner.hash_user_password(
        owner.Argon2idHashRequest("synthetic", parameters)
    )
    assert not auth_flow.verify_service_secret("synthetic", stored)


@pytest.mark.parametrize(
    "field,value",
    [
        ("time_cost", 0),
        ("time_cost", True),
        ("memory_cost_kib", 7),
        ("parallelism", 0),
        ("hash_length", 3),
        ("salt_length", 7),
    ],
)
def test_invalid_parameters_fail(field: str, value: int) -> None:
    values = dict(
        time_cost=1, memory_cost_kib=32, parallelism=1, hash_length=16, salt_length=16
    )
    values[field] = value
    with pytest.raises(owner.InvalidArgon2idParametersError):
        owner.Argon2idParameters(**values)


def test_compat_reference_uses_existing_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve = Mock(return_value="synthetic")
    monkeypatch.setattr(secret_service, "resolve_secret", resolve)
    result = auth_flow.verify_user_password_outcome(
        owner.UserPasswordVerification("synthetic", "env://SYNTHETIC")
    )
    assert result.accepted and not result.legacy_upgrade_candidate
    resolve.assert_called_once_with("env://SYNTHETIC")


def test_unknown_representation_never_reaches_compatibility() -> None:
    legacy = Mock(side_effect=AssertionError("legacy reached"))
    result = owner.verify_user_password(
        owner.UserPasswordVerification("synthetic", "unknown"), legacy_verify=legacy
    )
    assert result.status is owner.PasswordVerificationStatus.INVALID
    legacy.assert_not_called()


@pytest.mark.parametrize(
    "replacement", ["m=4294967295", "t=4294967295", "p=4294967295"]
)
def test_argon2_native_work_is_bounded(
    replacement: str,
    parameters: owner.Argon2idParameters,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = owner.hash_user_password(
        owner.Argon2idHashRequest("synthetic", parameters)
    )
    key = replacement[0]
    old = {"m": "m=32", "t": "t=1", "p": "p=1"}[key]
    stored = stored.replace(old, replacement)
    native = Mock(side_effect=AssertionError("native allocation reached"))
    monkeypatch.setattr(owner, "verify_secret", native)
    assert not auth_flow.verify_user_password(
        "synthetic",
        stored,
        argon2_limits=owner.Argon2idVerificationLimits(parameters, 256),
    )
    native.assert_not_called()


def test_argon2_requires_limits_and_bounds_encoding(
    parameters: owner.Argon2idParameters, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = owner.hash_user_password(
        owner.Argon2idHashRequest("synthetic", parameters)
    )
    native = Mock(side_effect=AssertionError("native allocation reached"))
    monkeypatch.setattr(owner, "verify_secret", native)
    assert not auth_flow.verify_user_password("synthetic", stored)
    assert not auth_flow.verify_user_password(
        "synthetic",
        stored,
        argon2_limits=owner.Argon2idVerificationLimits(parameters, 1),
    )
    native.assert_not_called()


@pytest.mark.parametrize("hash_length,salt_length", [(4, 16), (16, 8)])
def test_argon2_digest_and_salt_bounds_before_native(
    hash_length: int,
    salt_length: int,
    parameters: owner.Argon2idParameters,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = owner.hash_user_password(
        owner.Argon2idHashRequest("synthetic", parameters)
    )
    maximum = owner.Argon2idParameters(1, 32, 1, hash_length, salt_length)
    native = Mock(side_effect=AssertionError("native allocation reached"))
    monkeypatch.setattr(owner, "verify_secret", native)
    assert not auth_flow.verify_user_password(
        "synthetic",
        stored,
        argon2_limits=owner.Argon2idVerificationLimits(maximum, 256),
    )
    native.assert_not_called()


def test_malformed_argon2_with_limits_denied(
    parameters: owner.Argon2idParameters,
) -> None:
    assert not auth_flow.verify_user_password(
        "synthetic",
        "$argon2id$",
        argon2_limits=owner.Argon2idVerificationLimits(parameters, 256),
    )
