"""User-only password representation policy; no persistence or runtime cutover.

Service credentials must keep using their protocol-specific compatibility owner.
The caller owns recovery, notices, standing, and any future upgrade transaction.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from argon2 import Parameters, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import ARGON2_VERSION, Type, hash_secret, verify_secret


class ReversiblePasswordPolicy(str, Enum):
    COMPAT = "compat"
    REFUSE = "refuse"


class PasswordFormat(str, Enum):
    ARGON2ID = "argon2id"
    BCRYPT = "bcrypt"
    PBKDF2_SHA256 = "pbkdf2_sha256"
    SHA512_CRYPT = "sha512_crypt"
    REVERSIBLE = "reversible"
    UNKNOWN = "unknown"


class PasswordVerificationStatus(str, Enum):
    ACCEPTED = "accepted"
    INVALID = "invalid"
    REVERSIBLE_REFUSED = "reversible_refused"


@dataclass(frozen=True)
class UserPasswordVerification:
    password: str = field(repr=False)
    stored: str | None = field(repr=False)
    reversible_policy: ReversiblePasswordPolicy = ReversiblePasswordPolicy.COMPAT
    argon2_limits: Argon2idVerificationLimits | None = None


@dataclass(frozen=True)
class PasswordVerificationOutcome:
    status: PasswordVerificationStatus
    format: PasswordFormat

    @property
    def accepted(self) -> bool:
        return self.status is PasswordVerificationStatus.ACCEPTED

    @property
    def legacy_upgrade_candidate(self) -> bool:
        """Classification only: no upgrade permission or side effect."""
        return self.accepted and self.format in {
            PasswordFormat.BCRYPT,
            PasswordFormat.PBKDF2_SHA256,
            PasswordFormat.SHA512_CRYPT,
        }


def classify_password_format(stored: str | None) -> PasswordFormat:
    """Classify without decrypting or resolving references; not validation."""
    if not isinstance(stored, str):
        return PasswordFormat.UNKNOWN
    if stored.startswith(
        ("plain:", "enc:", "bao://", "openbao://", "vault://", "env://")
    ):
        return PasswordFormat.REVERSIBLE
    if stored.startswith("$argon2id$"):
        return PasswordFormat.ARGON2ID
    if stored.startswith(("$2a$", "$2b$", "$2y$")):
        return PasswordFormat.BCRYPT
    if stored.startswith("$pbkdf2-sha256$"):
        return PasswordFormat.PBKDF2_SHA256
    if stored.startswith("$6$"):
        return PasswordFormat.SHA512_CRYPT
    return PasswordFormat.UNKNOWN


def verify_user_password(
    request: UserPasswordVerification,
    *,
    legacy_verify: Callable[[str, str | None], bool],
) -> PasswordVerificationOutcome:
    """Apply policy before invoking any reversible compatibility operation."""
    representation = classify_password_format(request.stored)
    status = PasswordVerificationStatus.INVALID
    if not isinstance(request.reversible_policy, ReversiblePasswordPolicy):
        return PasswordVerificationOutcome(status, representation)
    if (
        representation is PasswordFormat.REVERSIBLE
        and request.reversible_policy is ReversiblePasswordPolicy.REFUSE
    ):
        return PasswordVerificationOutcome(
            PasswordVerificationStatus.REVERSIBLE_REFUSED, representation
        )
    if not isinstance(request.password, str) or not isinstance(request.stored, str):
        return PasswordVerificationOutcome(status, representation)
    try:
        if representation is PasswordFormat.ARGON2ID:
            limits = request.argon2_limits
            if limits is None or len(request.stored) > limits.max_encoded_length:
                return PasswordVerificationOutcome(status, representation)
            parameters = extract_parameters(request.stored)
            accepted = (
                limits.permits(parameters)
                and parameters.type is Type.ID
                and parameters.version == ARGON2_VERSION
                and verify_secret(
                    request.stored.encode("ascii"),
                    request.password.encode("utf-8"),
                    Type.ID,
                )
            )
        elif representation is PasswordFormat.UNKNOWN:
            accepted = False
        else:
            accepted = legacy_verify(request.password, request.stored)
    except (InvalidHashError, VerificationError, ValueError, TypeError, UnicodeError):
        accepted = False
    if accepted:
        status = PasswordVerificationStatus.ACCEPTED
    return PasswordVerificationOutcome(status, representation)


class InvalidArgon2idParametersError(ValueError):
    """The caller supplied no usable, explicit Argon2id parameter set."""

    code = "auth.user_passwords.invalid_argon2id_parameters"


@dataclass(frozen=True)
class Argon2idParameters:
    time_cost: int
    memory_cost_kib: int
    parallelism: int
    hash_length: int
    salt_length: int

    def __post_init__(self) -> None:
        values = (
            self.time_cost,
            self.memory_cost_kib,
            self.parallelism,
            self.hash_length,
            self.salt_length,
        )
        if (
            any(type(value) is not int or value < 1 for value in values)
            or self.memory_cost_kib < 8 * self.parallelism
            or self.hash_length < 4
            or self.salt_length < 8
            or any(value > 2**32 - 1 for value in values)
        ):
            raise InvalidArgon2idParametersError("Invalid explicit Argon2id parameters")


@dataclass(frozen=True)
class Argon2idVerificationLimits:
    """Caller-reviewed native allocation/work admission; no production defaults."""

    max_parameters: Argon2idParameters
    max_encoded_length: int

    def __post_init__(self) -> None:
        if (
            type(self.max_encoded_length) is not int
            or not 1 <= self.max_encoded_length <= 2**32 - 1
        ):
            raise InvalidArgon2idParametersError("Invalid Argon2id encoding limit")

    def permits(self, parameters: Parameters) -> bool:
        maximum = self.max_parameters
        return (
            1 <= parameters.time_cost <= maximum.time_cost
            and 1 <= parameters.parallelism <= maximum.parallelism
            and 8 * parameters.parallelism
            <= parameters.memory_cost
            <= maximum.memory_cost_kib
            and 4 <= parameters.hash_len <= maximum.hash_length
            and 8 <= parameters.salt_len <= maximum.salt_length
        )


@dataclass(frozen=True)
class Argon2idHashRequest:
    password: str = field(repr=False)
    parameters: Argon2idParameters


def hash_user_password(request: Argon2idHashRequest) -> str:
    """Create Argon2id v19 with a fresh salt and caller-selected parameters.

    There are deliberately no production parameter defaults and no callers in
    existing password creation/reset flows until benchmark and adoption gates.
    """
    parameters = request.parameters
    return hash_secret(
        secret=request.password.encode("utf-8"),
        salt=secrets.token_bytes(parameters.salt_length),
        time_cost=parameters.time_cost,
        memory_cost=parameters.memory_cost_kib,
        parallelism=parameters.parallelism,
        hash_len=parameters.hash_length,
        type=Type.ID,
        version=ARGON2_VERSION,
    ).decode("ascii")
