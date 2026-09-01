#!/usr/bin/python3 -I
"""Persistent root-owned rollback executor for the legacy image-pin bootstrap.

Installed under ``/usr/local/libexec``.  It uses only the standard library so
the rollback path survives loss of the Actions checkout, the runner process and
a reboot.  It never prints environment values or Docker inspection documents.

It differs from the steady-state deadman in one load-bearing way.  Rolling back
restores the LISTENER preimage but KEEPS the immutable image reference: the
bytes are identical either way -- the digest names the image that was already
running -- and reverting to the mutable tag would put the service back in the
state that ordinary v2 PLAN/APPLY refuses to touch, which is the exact
condition the bootstrap exists to remove.  A rolled-back bootstrap has
therefore still achieved its durable purpose, so it writes a terminal receipt
and refuses to be repeated.
"""

from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

STATE_ROOT = Path("/var/lib/dotmac/legacy-image-pin")
RECEIPT_NAME = "receipt.json"
OPERATION = re.compile(r"^imagepin-postgres-local-[1-9][0-9]*$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
DIGEST_REFERENCE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
LEGACY_TAG = re.compile(
    r"^[a-z0-9]+(?:[._\-/][a-z0-9]+)*:[A-Za-z0-9_][A-Za-z0-9._\-]{0,127}$"
)
SERVICE = "postgres-local"
BIND_KEY = "PG_LOCAL_BIND"
TERMINAL_REASONS = {
    "runner-failure",
    "signal",
    "timeout",
    "postcondition-failure",
    "operator-request",
    "verified-success",
}
STATE_FIELDS = {
    "schema",
    "operation_id",
    "plan_digest",
    "service",
    "deploy_dir",
    "env_file",
    "docker_bin",
    "compose_files",
    "retained_image_reference",
    "before_image_id",
    "bind_env",
    "bind_was_present",
    "bind_preimage",
    "before_container_id",
    "before_listeners",
    "deadline",
    "state",
    "state_reason",
    "updated_at",
}


class DeadmanError(RuntimeError):
    pass


def _fail(message: str) -> Never:
    raise DeadmanError(message)


def _canonical(document: dict[str, object]) -> bytes:
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        _fail(f"{field} is not an ISO timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(f"{field} is not timezone-aware")
    return parsed.astimezone(UTC)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _listener_keys(rows: object, label: str) -> list[tuple[int, str, int, str]]:
    if not isinstance(rows, list) or not rows:
        _fail(f"{label} is empty")
    keys: list[tuple[int, str, int, str]] = []
    for item in rows:
        if not isinstance(item, dict) or set(item) != {
            "container_port",
            "host_ip",
            "host_port",
            "protocol",
        }:
            _fail(f"invalid {label} row")
        container_port = item["container_port"]
        host_port = item["host_port"]
        protocol = item["protocol"]
        if (
            not isinstance(container_port, int)
            or isinstance(container_port, bool)
            or not 1 <= container_port <= 65535
            or not isinstance(host_port, int)
            or isinstance(host_port, bool)
            or not 1 <= host_port <= 65535
            or protocol not in {"tcp", "udp"}
        ):
            _fail(f"invalid {label} socket")
        try:
            host_ip = str(ipaddress.ip_address(item["host_ip"]))
        except ValueError as error:
            raise DeadmanError(f"invalid {label} address") from error
        keys.append((container_port, host_ip, host_port, str(protocol)))
    if keys != sorted(set(keys)):
        _fail(f"{label} is not unique and sorted")
    return keys


def _validate_state(document: object, operation: str) -> dict[str, object]:
    if not isinstance(document, dict):
        _fail("state document is not an object")
    if set(document) != STATE_FIELDS:
        _fail("state fields differ from LegacyImagePinBootstrapDeadmanStateV1")
    if document["schema"] != "LegacyImagePinBootstrapDeadmanStateV1":
        _fail("unsupported deadman state schema")
    if document["operation_id"] != operation or not OPERATION.fullmatch(operation):
        _fail("deadman operation identity differs")
    if document["service"] != SERVICE:
        _fail("the bootstrap deadman serves only postgres-local")
    if document["bind_env"] != BIND_KEY:
        _fail("the bootstrap deadman restores only the declared bind variable")
    if document["state"] not in {"armed", "rolled_back", "disarmed"}:
        _fail("unknown deadman state")
    reason = document["state_reason"]
    if document["state"] == "armed" and reason is not None:
        _fail("an armed deadman state cannot carry a terminal reason")
    if document["state"] != "armed" and reason not in TERMINAL_REASONS:
        _fail("a terminal deadman state has an unsupported reason")
    for field in ("plan_digest", "before_image_id"):
        value = document[field]
        if not isinstance(value, str) or not DIGEST.fullmatch(value):
            _fail(f"deadman {field} is invalid")
    retained = document["retained_image_reference"]
    if not isinstance(retained, str) or not DIGEST_REFERENCE.fullmatch(retained):
        _fail("the retained image reference must be an immutable digest")
    before = document["before_container_id"]
    if not isinstance(before, str) or not CONTAINER_ID.fullmatch(before):
        _fail("deadman container ID is invalid")
    if not isinstance(document["bind_was_present"], bool) or not isinstance(
        document["bind_preimage"], str
    ):
        _fail("invalid bind preimage")
    if not document["bind_was_present"] and document["bind_preimage"]:
        _fail("an absent bind variable carries no prior value")
    _parse_time(document["deadline"], "deadline")
    _parse_time(document["updated_at"], "updated_at")
    for field in ("deploy_dir", "env_file", "docker_bin"):
        value = document[field]
        if not isinstance(value, str) or not Path(value).is_absolute():
            _fail(f"{field} must be an absolute path")
    docker_bin = Path(str(document["docker_bin"]))
    docker_stat = docker_bin.stat()
    if (
        not docker_bin.is_file()
        or not os.access(docker_bin, os.X_OK)
        or docker_stat.st_uid != 0
        or docker_stat.st_mode & 0o022
    ):
        _fail("deadman Docker binary must be root-owned, executable and non-writable")
    compose_files = document["compose_files"]
    if not isinstance(compose_files, list) or not compose_files:
        _fail("at least one compose file is required")
    if any(
        not isinstance(value, str) or not Path(value).is_absolute()
        for value in compose_files
    ):
        _fail("compose file paths must be absolute")
    _listener_keys(document["before_listeners"], "deadman listener preimage")
    return document


def _state_path(operation: str) -> Path:
    if not OPERATION.fullmatch(operation):
        _fail("invalid operation identity")
    return STATE_ROOT / operation / "state.json"


@contextmanager
def _locked_state(operation: str) -> Iterator[tuple[Path, dict[str, object]]]:
    path = _state_path(operation)
    if not path.is_file():
        _fail("deadman state is absent")
    lock_path = path.with_name("state.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        raw = path.read_bytes()
        document = _validate_state(json.loads(raw), operation)
        if raw != _canonical(document):
            _fail("deadman state is not canonical")
        yield path, document


def _atomic_write(path: Path, document: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(document))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _restore_bind(document: dict[str, object]) -> None:
    """Put PG_LOCAL_BIND back exactly as it was, including absent."""

    path = Path(str(document["env_file"]))
    stat = path.stat()
    lines = path.read_text(encoding="utf-8").splitlines()
    retained = [line for line in lines if not line.startswith(f"{BIND_KEY}=")]
    if document["bind_was_present"]:
        retained.append(f"{BIND_KEY}={document['bind_preimage']}")
    temporary = path.with_name(f".{path.name}.imagepin-deadman-{os.getpid()}")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.st_mode & 0o777
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(retained) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, stat.st_uid, stat.st_gid)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _compose_prefix(document: dict[str, object]) -> list[str]:
    command = [
        str(document["docker_bin"]),
        "compose",
        "--project-directory",
        str(document["deploy_dir"]),
        "--env-file",
        str(document["env_file"]),
    ]
    for path in document["compose_files"]:
        command.extend(("-f", str(path)))
    return command


def _recreate_command(document: dict[str, object]) -> list[str]:
    return [
        *_compose_prefix(document),
        "up",
        "-d",
        "--no-deps",
        "--no-build",
        "--pull",
        "never",
        "--force-recreate",
        SERVICE,
    ]


def _observed_target(
    document: dict[str, object],
) -> tuple[str, str, str, list[tuple[int, str, int, str]]]:
    prefix = _compose_prefix(document)
    result = subprocess.run(
        [*prefix, "ps", "-q", SERVICE], check=True, text=True, capture_output=True
    )
    ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(ids) != 1:
        _fail("rollback did not leave exactly one target container")
    inspected = subprocess.run(
        [
            str(document["docker_bin"]),
            "inspect",
            ids[0],
            "--format",
            '{"container_id":{{json .Id}},"image_id":{{json .Image}},"image_reference":{{json .Config.Image}},"ports":{{json .NetworkSettings.Ports}}}',
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    observation = json.loads(inspected.stdout)
    if not isinstance(observation, dict) or set(observation) != {
        "container_id",
        "image_id",
        "image_reference",
        "ports",
    }:
        _fail("rollback target observation has unexpected fields")
    ports = observation["ports"]
    if not isinstance(ports, dict):
        _fail("rollback target ports are not an object")
    keys: list[tuple[int, str, int, str]] = []
    for port_spec, bindings in (ports or {}).items():
        container_port, _, protocol = str(port_spec).partition("/")
        for binding in bindings or ():
            keys.append(
                (
                    int(container_port),
                    str(binding["HostIp"]),
                    int(binding["HostPort"]),
                    protocol,
                )
            )
    return (
        str(observation["container_id"]).removeprefix("sha256:"),
        str(observation["image_id"]),
        str(observation["image_reference"]),
        sorted(keys),
    )


def _receipt_path() -> Path:
    return STATE_ROOT / RECEIPT_NAME


def _write_rollback_receipt(
    document: dict[str, object], after_container_id: str
) -> None:
    """The terminal record that stops this bootstrap being repeated.

    A rollback is still a terminal outcome for the operation: the immutable
    reference is retained, so the ordinary v2 lane can now own the listener
    correction, and a second bootstrap would only buy another unreviewed
    recreate.
    """

    path = _receipt_path()
    if path.exists():
        return
    operation = str(document["operation_id"])
    receipt = {
        "schema": "LegacyImagePinBootstrapRollbackReceiptV1",
        "outcome": "rolled_back",
        "operation_id": operation,
        "service": SERVICE,
        "plan_digest": document["plan_digest"],
        "retained_image_reference": document["retained_image_reference"],
        "image_id": document["before_image_id"],
        "before_container_id": document["before_container_id"],
        "after_container_id": after_container_id,
        "state_reason": document["state_reason"],
        "recorded_at": _now(),
    }
    temporary = path.with_name(f".{RECEIPT_NAME}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(receipt))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _rollback(document: dict[str, object], reason: str) -> None:
    if document["state"] != "armed":
        return
    if reason not in TERMINAL_REASONS - {"verified-success"}:
        _fail("unsupported rollback reason")
    _restore_bind(document)
    subprocess.run(_recreate_command(document), check=True)
    expected = _listener_keys(document["before_listeners"], "deadman listener preimage")
    after_id, image_id, reference, listeners = _observed_target(document)
    if image_id != document["before_image_id"]:
        _fail("the rolled-back container does not run the prior image ID")
    if reference != document["retained_image_reference"]:
        # The pin is deliberately RETAINED across a rollback; losing it would
        # return the service to the state the steady-state lane cannot touch.
        _fail("the rolled-back container did not retain the immutable reference")
    if listeners != expected:
        _fail("the rolled-back container listeners do not match the preimage")
    document["state"] = "rolled_back"
    document["state_reason"] = reason
    document["updated_at"] = _now()
    _write_rollback_receipt(document, after_id)


def check(operation: str) -> None:
    with _locked_state(operation) as (path, document):
        if document["state"] != "armed":
            return
        if datetime.now(UTC) < _parse_time(document["deadline"], "deadline"):
            return
        _rollback(document, "timeout")
        _atomic_write(path, document)


def rollback_now(operation: str, reason: str) -> None:
    with _locked_state(operation) as (path, document):
        _rollback(document, reason)
        _atomic_write(path, document)


def disarm(operation: str) -> None:
    with _locked_state(operation) as (path, document):
        if document["state"] != "armed":
            _fail("only an armed deadman can be disarmed")
        document["state"] = "disarmed"
        document["state_reason"] = "verified-success"
        document["updated_at"] = _now()
        _atomic_write(path, document)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "disarm", "validate"):
        command = commands.add_parser(name)
        command.add_argument("--operation", required=True)
    rollback = commands.add_parser("rollback-now")
    rollback.add_argument("--operation", required=True)
    rollback.add_argument(
        "--reason",
        required=True,
        choices=sorted(TERMINAL_REASONS - {"verified-success"}),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "check":
            check(args.operation)
        elif args.command == "rollback-now":
            rollback_now(args.operation, args.reason)
        elif args.command == "disarm":
            disarm(args.operation)
        elif args.command == "validate":
            with _locked_state(args.operation):
                pass
    except (DeadmanError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"LEGACY IMAGE PIN DEADMAN FAILED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
