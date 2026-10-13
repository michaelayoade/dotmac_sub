"""Read-only candidate boot-material readiness; never report secret values."""

from __future__ import annotations

import argparse
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from cryptography.fernet import Fernet

from app.services.kernel_key_provider import KEYRING_REF, parse_keyring
from app.services.kernel_secret_source import OPTIONAL_SECRET_REFS, SECRET_REFS
from app.services.secrets import resolve_openbao_ref, resolve_openbao_ref_optional

REQUIRED_REFS: Mapping[str, str] = {
    **SECRET_REFS,
    "settings_encryption_keyring": KEYRING_REF,
}
OPTIONAL_REFS: Mapping[str, str] = OPTIONAL_SECRET_REFS


class BootSecretStatus(StrEnum):
    READY = "ready"
    ABSENT = "absent"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class BootSecretCheck:
    name: str
    status: BootSecretStatus


@dataclass(frozen=True, slots=True)
class BootSecretPreflightResult:
    """Bounded field-name/status evidence, containing no material or errors."""

    checks: tuple[BootSecretCheck, ...]

    @property
    def checked_names(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks)

    @property
    def failed_names(self) -> tuple[str, ...]:
        return tuple(
            check.name
            for check in self.checks
            if check.status in (BootSecretStatus.INVALID, BootSecretStatus.UNAVAILABLE)
        )

    @property
    def absent_names(self) -> tuple[str, ...]:
        return tuple(
            check.name
            for check in self.checks
            if check.status is BootSecretStatus.ABSENT
        )

    @property
    def ok(self) -> bool:
        return not self.failed_names


def _check(
    name: str,
    reference: str,
    resolver: Callable[[str], str | None],
    *,
    required: bool,
) -> BootSecretCheck:
    try:
        value = resolver(reference)
    except Exception:  # Exceptions can contain response bodies or material.
        return BootSecretCheck(name, BootSecretStatus.UNAVAILABLE)
    if value is None:
        return BootSecretCheck(
            name, BootSecretStatus.INVALID if required else BootSecretStatus.ABSENT
        )
    try:
        if not value.strip():
            return BootSecretCheck(name, BootSecretStatus.INVALID)
        if name in (
            "credential_encryption_key",
            "totp_encryption_key",
            "wireguard_key_encryption_key",
        ):
            # These owners instantiate Fernet from ASCII boot material.
            Fernet(value.encode("ascii"))
        if reference == KEYRING_REF:
            keyring = parse_keyring(value)
            active = keyring.active()
            if active is None:
                return BootSecretCheck(name, BootSecretStatus.INVALID)
            # Runtime uses this same cipher constructor. Do not install the
            # kernel's global provider or populate its material-keyed cache.
            Fernet(active.material.encode())
    except Exception:  # Neither parser diagnostics nor key ids are evidence.
        return BootSecretCheck(name, BootSecretStatus.INVALID)
    return BootSecretCheck(name, BootSecretStatus.READY)


def check_required_boot_secrets(
    refs: Mapping[str, str] = REQUIRED_REFS,
    resolver: Callable[[str], str] = resolve_openbao_ref,
) -> BootSecretPreflightResult:
    """Validate mandatory fields, including usable settings-write material."""
    return BootSecretPreflightResult(
        tuple(
            _check(name, reference, resolver, required=True)
            for name, reference in refs.items()
        )
    )


def check_optional_boot_secrets(
    refs: Mapping[str, str] = OPTIONAL_REFS,
    resolver: Callable[[str], str | None] = resolve_openbao_ref_optional,
    *,
    required_names: tuple[str, ...] = (),
) -> BootSecretPreflightResult:
    """Only missing paths may be absent; all other optional failures gate.

    A feature owner may require names from the canonical optional registry.
    Unknown names are a contract error, never silently skipped.
    """
    if set(required_names) - refs.keys():
        raise ValueError("Required optional names must belong to the boot registry")
    return BootSecretPreflightResult(
        tuple(
            _check(name, reference, resolver, required=name in required_names)
            for name, reference in refs.items()
        )
    )


def check_boot_secrets(
    *,
    required_optional_names: tuple[str, ...] = (),
    required_resolver: Callable[[str], str] = resolve_openbao_ref,
    optional_resolver: Callable[[str], str | None] = resolve_openbao_ref_optional,
) -> BootSecretPreflightResult:
    """Check both boot registries without installing process-held material."""
    required = check_required_boot_secrets(resolver=required_resolver)
    optional = check_optional_boot_secrets(
        resolver=optional_resolver, required_names=required_optional_names
    )
    return BootSecretPreflightResult(required.checks + optional.checks)


def _component_name(raw: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", raw):
        raise argparse.ArgumentTypeError("Component must be a bounded service name")
    return raw


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", type=_component_name, default="app")
    args = parser.parse_args(argv)
    # Secret transports may log URLs and exception bodies before raising.
    # A standalone probe emits only its bounded receipt and restores the
    # caller's logging state even when evaluation raises unexpectedly.
    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        result = check_boot_secrets()
    finally:
        logging.disable(previous_logging_disable)
    for check in result.checks:
        print(
            f"OpenBao boot-secret {args.component}/{check.name}: {check.status.value}"
        )
    if not result.ok:
        print(
            f"OpenBao boot-secret preflight failed for: {', '.join(result.failed_names)}"
        )
        return 1
    print(f"OpenBao boot-secret preflight passed for {len(result.checks)} fields.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
