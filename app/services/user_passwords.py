"""User-only password representation policy; no persistence or runtime cutover.

Service credentials must keep using their protocol-specific compatibility owner.
The caller owns recovery, notices, standing, and any future upgrade transaction.
"""

from __future__ import annotations

import math
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum

from argon2 import Parameters, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import ARGON2_VERSION, Type, hash_secret, verify_secret


class PasswordAdmissionErrorCode(str, Enum):
    INVALID_CONFIGURATION = "auth.user_passwords.invalid_admission_configuration"
    SATURATED = "auth.user_passwords.admission_saturated"
    TIMED_OUT = "auth.user_passwords.admission_timed_out"


class InvalidLimiterConfigurationError(ValueError):
    code = PasswordAdmissionErrorCode.INVALID_CONFIGURATION


class PasswordAdmissionBusyError(RuntimeError):
    """Expected admission refusal; adapters own mapping and any retry decision."""

    def __init__(self, code: PasswordAdmissionErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True)
class LimiterConfiguration:
    """Explicit per-process budget, supplied only after deployment review."""

    k: int
    maximum_waiters: int
    admission_timeout: float
    threadpool_tokens: int
    reserved_tokens: int

    def __post_init__(self) -> None:
        counts = (
            self.k,
            self.maximum_waiters,
            self.threadpool_tokens,
            self.reserved_tokens,
        )
        if (
            any(type(value) is not int or value < 0 for value in counts)
            or self.k < 1
            or self.threadpool_tokens < 1
            or self.k + self.maximum_waiters
            > self.threadpool_tokens - self.reserved_tokens
        ):
            raise InvalidLimiterConfigurationError("Invalid explicit admission budget")
        try:
            valid_timeout = (
                type(self.admission_timeout) in (int, float)
                and math.isfinite(self.admission_timeout)
                and self.admission_timeout >= 0
            )
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise InvalidLimiterConfigurationError("Invalid explicit admission timeout")


@dataclass(frozen=True)
class PasswordAdmissionOutcome:
    """One admitted scope's evidence, containing no identity or password data."""

    queue_wait_seconds: float


class NativePasswordAdmission:
    """Dormant synchronous admission primitive; no native operation is installed.

    Share one explicitly constructed instance within a process after adoption.
    This does not coordinate processes, resize/enforce a framework threadpool,
    promise FIFO fairness, or cancel native work. Acquire separately for verify
    and any later rehash, releasing between operations.
    """

    def __init__(self, configuration: LimiterConfiguration) -> None:
        if not isinstance(configuration, LimiterConfiguration):
            raise InvalidLimiterConfigurationError("Typed admission budget required")
        self._configuration = configuration
        self._condition = threading.Condition()
        self._active = 0
        self._waiting = 0

    @property
    def configuration(self) -> LimiterConfiguration:
        return self._configuration

    @contextmanager
    def admit(self) -> Iterator[PasswordAdmissionOutcome]:
        """Admit one scope or raise busy; release even on BaseException.

        A bounded waiter consumes a caller thread. Excess callers are refused
        immediately after taking the short accounting lock. Never hold this
        scope across unrelated work, database transactions, or another admit.
        """
        started = time.monotonic()
        admitted = False
        waiting = False
        try:
            with self._condition:
                try:
                    if self._active >= self.configuration.k:
                        if self._waiting >= self.configuration.maximum_waiters:
                            raise PasswordAdmissionBusyError(
                                PasswordAdmissionErrorCode.SATURATED
                            )
                        self._waiting += 1
                        waiting = True
                        while True:
                            remaining = self.configuration.admission_timeout - (
                                time.monotonic() - started
                            )
                            if remaining <= 0:
                                raise PasswordAdmissionBusyError(
                                    PasswordAdmissionErrorCode.TIMED_OUT
                                )
                            if self._active < self.configuration.k:
                                break
                            self._condition.wait(min(remaining, threading.TIMEOUT_MAX))
                    self._active += 1
                    admitted = True
                finally:
                    if waiting:
                        self._waiting -= 1
            yield PasswordAdmissionOutcome(time.monotonic() - started)
        finally:
            if admitted:
                with self._condition:
                    self._active -= 1
                    self._condition.notify_all()


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
