"""Finite staging proof runner install; registration and activation stay human gates."""

from __future__ import annotations

import hashlib
import io
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from deploy.agent.provision import (
    CapturedInstall,
    ProvisionError,
    Target,
    command,
    host_guard,
    parents,
    protected,
)

ROOT = Path("/opt/dotmac-proof1-runner")
UNIT = "actions.runner.michaelayoade-dotmac_sub.Seabone-proof1.service"


@dataclass(frozen=True)
class RunnerPackage:
    archive: bytes = field(repr=False)
    sha256: str
    revision: str
    # Public signed release statement + separately reviewed pinned signer.
    statement_signature: bytes = field(repr=False)
    trusted_signer: bytes


def install(package: RunnerPackage, *, target: Target) -> str:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    host_guard(target)
    if (
        target is not Target.SEABONE
        or hashlib.sha256(package.archive).hexdigest() != package.sha256
    ):
        raise ProvisionError("runner_pin_invalid")
    statement = f"https://github.com/actions/runner\n{package.revision}\n{package.sha256}".encode()
    try:
        Ed25519PublicKey.from_public_bytes(package.trusted_signer).verify(
            package.statement_signature, statement
        )
    except (ValueError, InvalidSignature):
        raise ProvisionError("runner_signature_invalid") from None
    transaction = CapturedInstall()
    try:
        with tarfile.open(fileobj=io.BytesIO(package.archive), mode="r:gz") as archive:
            seen: set[str] = set()
            entrypoints: set[str] = set()
            total = 0
            members = archive.getmembers()
            if len(members) > 30000:
                raise ProvisionError("runner_archive_bounds")
            for member in members:
                name = PurePosixPath(member.name)
                canonical = str(name)
                total += member.size
                if (
                    name.is_absolute()
                    or ".." in name.parts
                    or not name.parts
                    or canonical in seen
                    or not (member.isfile() or member.isdir())
                    or total > 2_000_000_000
                ):
                    raise ProvisionError("runner_archive_unsafe")
                seen.add(canonical)
                member.name = canonical
                if member.isfile() and member.mode & 0o111:
                    entrypoints.add(canonical)
            if not {"config.sh", "run.sh", "svc.sh"}.issubset(entrypoints):
                raise ProvisionError("runner_archive_incomplete")
            for member in members:
                path = ROOT / member.name
                if member.isdir():
                    transaction.directory(path)
                else:
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ProvisionError("runner_archive_incomplete")
                    transaction.install(
                        path,
                        stream.read(),
                        mode=0o755 if member.mode & 0o111 else 0o644,
                    )
        return package.sha256
    except BaseException:
        transaction.rollback()
        raise


@dataclass(frozen=True)
class RegistrationReceipt:
    repository: str
    label: str
    name: str
    unit_sha256: str


def stop_before_proofs34(*, target: Target, receipt: RegistrationReceipt) -> None:
    host_guard(target)
    if target is not Target.SEABONE:
        raise ProvisionError("runner_target_invalid")
    # Registration/install service is a separately reviewed human-owned step.
    unit = Path("/etc/systemd/system") / UNIT
    parents(unit)
    protected(unit)
    if (
        receipt.repository != "michaelayoade/dotmac_sub"
        or receipt.label != "dotmac-sub-staging"
        or receipt.name != "Seabone-proof1"
        or hashlib.sha256(unit.read_bytes()).hexdigest() != receipt.unit_sha256
    ):
        raise ProvisionError("runner_registration_receipt_invalid")
    command(("/usr/bin/systemctl", "stop", UNIT))
    state = command(
        ("/usr/bin/systemctl", "show", UNIT, "--property=ActiveState", "--value")
    )
    if state != b"inactive\n":
        raise ProvisionError("runner_stop_unproven")
