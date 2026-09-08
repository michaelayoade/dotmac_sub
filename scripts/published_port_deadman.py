#!/usr/bin/python3 -I
"""Persistent root-owned rollback executor for published-port reconcile v2.

This file is installed under ``/usr/local/libexec``.  It intentionally uses
only the Python standard library so the rollback path survives loss of the
Actions checkout and its virtual environment.  It never prints environment
values, container configuration, or Docker inspection documents.
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

STATE_DIR = Path("/var/lib/dotmac/published-port-reconcile")
OPERATION = re.compile(r"^port-[a-z0-9-]+-[1-9][0-9]*$")
ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*_BIND$")
SERVICE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
IMAGE_REFERENCE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
TERMINAL_REASONS = {
    "runner-failure",
    "signal",
    "timeout",
    "postcondition-failure",
    "operator-request",
    "verified-success",
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


def _validate_state(document: object, operation: str) -> dict[str, object]:
    if not isinstance(document, dict):
        _fail("state document is not an object")
    required = {
        "schema",
        "operation_id",
        "execution_plan_digest",
        "service",
        "deploy_dir",
        "env_file",
        "docker_bin",
        "compose_files",
        "image_reference",
        "before_image_id",
        "env_preimage",
        "before_container_id",
        "before_listeners",
        "deadline",
        "state",
        "state_reason",
        "updated_at",
    }
    if set(document) != required:
        _fail("state document fields differ from PublishedPortDeadmanStateV2")
    if document["schema"] != "PublishedPortDeadmanStateV2":
        _fail("unsupported deadman state schema")
    if document["operation_id"] != operation or not OPERATION.fullmatch(operation):
        _fail("deadman operation identity differs")
    if document["state"] not in {"armed", "rolled_back", "disarmed"}:
        _fail("unknown deadman state")
    reason = document["state_reason"]
    if document["state"] == "armed" and reason is not None:
        _fail("armed deadman state cannot carry a terminal reason")
    if document["state"] != "armed" and reason not in TERMINAL_REASONS:
        _fail("terminal deadman state has an unsupported reason")
    service = document["service"]
    if not isinstance(service, str) or not SERVICE.fullmatch(service):
        _fail("deadman service identity is invalid")
    if not re.fullmatch(rf"port-{re.escape(service)}-[1-9][0-9]*", operation):
        _fail("deadman operation does not bind its service")
    for field in ("execution_plan_digest", "before_image_id"):
        if not isinstance(document[field], str) or not DIGEST.fullmatch(
            document[field]
        ):
            _fail(f"deadman {field} is invalid")
    if not isinstance(
        document["image_reference"], str
    ) or not IMAGE_REFERENCE.fullmatch(document["image_reference"]):
        _fail("deadman image reference is invalid")
    if not isinstance(
        document["before_container_id"], str
    ) or not CONTAINER_ID.fullmatch(document["before_container_id"]):
        _fail("deadman container ID is invalid")
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
    preimage = document["env_preimage"]
    if not isinstance(preimage, list) or not preimage:
        _fail("environment preimage is empty")
    keys: list[str] = []
    for item in preimage:
        if not isinstance(item, dict) or set(item) != {"key", "present", "value"}:
            _fail("invalid environment preimage row")
        key = item["key"]
        if not isinstance(key, str) or not ENV_KEY.fullmatch(key):
            _fail("deadman may restore bind variables only")
        if not isinstance(item["present"], bool) or not isinstance(item["value"], str):
            _fail("invalid environment preimage value")
        if not item["present"] and item["value"]:
            _fail("absent environment key carries a value")
        keys.append(key)
    if keys != sorted(set(keys)):
        _fail("environment preimage is not unique and sorted")
    listeners = document["before_listeners"]
    if not isinstance(listeners, list) or not listeners:
        _fail("deadman listener preimage is empty")
    listener_keys: list[tuple[int, str, int, str]] = []
    for item in listeners:
        if not isinstance(item, dict) or set(item) != {
            "container_port",
            "host_ip",
            "host_port",
            "protocol",
        }:
            _fail("invalid deadman listener preimage row")
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
            _fail("invalid deadman listener socket")
        try:
            host_ip = str(ipaddress.ip_address(item["host_ip"]))
        except ValueError as error:
            raise DeadmanError("invalid deadman listener address") from error
        listener_keys.append((container_port, host_ip, host_port, str(protocol)))
    if listener_keys != sorted(set(listener_keys)):
        _fail("deadman listener preimage is not unique and sorted")
    return document


def _state_path(operation: str) -> Path:
    if not OPERATION.fullmatch(operation):
        _fail("invalid operation identity")
    return STATE_DIR / operation / "state.json"


@contextmanager
def _locked_state(operation: str) -> Iterator[tuple[Path, dict[str, object]]]:
    path = _state_path(operation)
    if not path.is_file():
        _fail("deadman state is absent")
    lock_path = path.with_name("state.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with os.fdopen(descriptor, "r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            raw = path.read_bytes()
            document = _validate_state(json.loads(raw), operation)
            if raw != _canonical(document):
                _fail("deadman state is not canonical")
            yield path, document
    finally:
        pass


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


def _restore_env(document: dict[str, object]) -> None:
    path = Path(str(document["env_file"]))
    stat = path.stat()
    lines = path.read_text(encoding="utf-8").splitlines()
    preimage = {row["key"]: row for row in document["env_preimage"]}
    retained = [
        line
        for line in lines
        if not any(line.startswith(f"{key}=") for key in preimage)
    ]
    for key in sorted(preimage):
        item = preimage[key]
        if item["present"]:
            retained.append(f"{key}={item['value']}")
    temporary = path.with_name(f".{path.name}.deadman-{os.getpid()}")
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


def _compose_command(document: dict[str, object]) -> list[str]:
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
    command.extend(
        (
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "--force-recreate",
            str(document["service"]),
        )
    )
    return command


def _observed_target(
    document: dict[str, object],
) -> tuple[str, str, list[tuple[int, str, int, str]]]:
    compose = _compose_command(document)
    prefix = compose[: compose.index("up")]
    result = subprocess.run(
        [*prefix, "ps", "-q", str(document["service"])],
        check=True,
        text=True,
        capture_output=True,
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
            '{"image_id":{{json .Image}},"image_reference":{{json .Config.Image}},"ports":{{json .NetworkSettings.Ports}}}',
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    observation = json.loads(inspected.stdout)
    if not isinstance(observation, dict) or set(observation) != {
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
        container_port, _, protocol = port_spec.partition("/")
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
        str(observation["image_id"]),
        str(observation["image_reference"]),
        sorted(keys),
    )


def _rollback(document: dict[str, object], reason: str) -> None:
    if document["state"] != "armed":
        return
    if reason not in TERMINAL_REASONS - {"verified-success"}:
        _fail("unsupported rollback reason")
    _restore_env(document)
    subprocess.run(_compose_command(document), check=True)
    expected = sorted(
        (
            int(item["container_port"]),
            str(item["host_ip"]),
            int(item["host_port"]),
            str(item["protocol"]),
        )
        for item in document["before_listeners"]
    )
    image_id, image_reference, listeners = _observed_target(document)
    if image_id != document["before_image_id"]:
        _fail("rollback container did not restore the image ID")
    if image_reference != document["image_reference"]:
        _fail("rollback container did not restore the immutable image reference")
    if listeners != expected:
        _fail("rollback container listeners do not match the preimage")
    document["state"] = "rolled_back"
    document["state_reason"] = reason
    document["updated_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")


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
        document["updated_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        _atomic_write(path, document)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "disarm"):
        command = commands.add_parser(name)
        command.add_argument("--operation", required=True)
    rollback = commands.add_parser("rollback-now")
    rollback.add_argument("--operation", required=True)
    rollback.add_argument(
        "--reason",
        required=True,
        choices=sorted(TERMINAL_REASONS - {"verified-success"}),
    )
    validate = commands.add_parser("validate")
    validate.add_argument("--operation", required=True)
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
        print(f"PUBLISHED PORT DEADMAN FAILED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
