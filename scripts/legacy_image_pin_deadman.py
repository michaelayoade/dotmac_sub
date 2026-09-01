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
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
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
REPLICATION_STANDBY = "75.119.157.91"
DECLARED_HOST_IP = "0.0.0.0"  # noqa: S104 - declared, source-restricted
DECLARED_HOST_PORT = 9001
DECLARED_CONTAINER_PORT = 5432
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
    "forward_bind",
    "forward_listeners",
    "volume_identity_digest",
    "before_container_id",
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
    if document["state"] not in {"armed", "recovered_forward", "disarmed"}:
        _fail("unknown deadman state")
    reason = document["state_reason"]
    if document["state"] == "armed" and reason is not None:
        _fail("an armed deadman state cannot carry a terminal reason")
    if document["state"] != "armed" and reason not in TERMINAL_REASONS:
        _fail("a terminal deadman state has an unsupported reason")
    for field in ("plan_digest", "before_image_id", "volume_identity_digest"):
        value = document[field]
        if not isinstance(value, str) or not DIGEST.fullmatch(value):
            _fail(f"deadman {field} is invalid")
    retained = document["retained_image_reference"]
    if not isinstance(retained, str) or not DIGEST_REFERENCE.fullmatch(retained):
        _fail("the retained image reference must be an immutable digest")
    before = document["before_container_id"]
    if not isinstance(before, str) or not CONTAINER_ID.fullmatch(before):
        _fail("deadman container ID is invalid")
    if document["forward_bind"] != f"{DECLARED_HOST_IP}:":
        _fail("the forward bind target is not the declared IPv4 wildcard")
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
    forward = _listener_keys(document["forward_listeners"], "deadman forward target")
    if forward != [
        (DECLARED_CONTAINER_PORT, DECLARED_HOST_IP, DECLARED_HOST_PORT, "tcp")
    ]:
        _fail(
            "the forward target is exactly one IPv4 listener; a dual-family "
            "listener is the vulnerability, not a recovery state"
        )
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


def _ensure_forward_bind(document: dict[str, object]) -> None:
    """Drive PG_LOCAL_BIND to the FORWARD target, never to a preimage.

    Setting it rather than restoring it is the whole inversion: the value this
    operation is recovering toward is the declared IPv4 wildcard, and the state
    it is recovering FROM (an absent variable, which the release Compose
    resolves to loopback) would strand the replication standby.
    """

    path = Path(str(document["env_file"]))
    stat = path.stat()
    lines = path.read_text(encoding="utf-8").splitlines()
    retained = [line for line in lines if not line.startswith(f"{BIND_KEY}=")]
    retained.append(f"{BIND_KEY}={document['forward_bind']}")
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


def _volume_identity(mounts: object) -> str:
    """A stable fingerprint of what the container has mounted.

    A recreate that preserves the container-ID discipline and the pinned image
    but silently re-binds a volume would pass every other check here, and the
    thing it would have moved is the data.
    """

    if not isinstance(mounts, list):
        _fail("container mounts are not a list")
    rows = []
    for item in mounts:
        if not isinstance(item, dict):
            _fail("a container mount is not an object")
        rows.append(
            {
                "type": str(item.get("Type", "")),
                "name": str(item.get("Name", "")),
                "source": str(item.get("Source", "")),
                "destination": str(item.get("Destination", "")),
                "rw": bool(item.get("RW", False)),
            }
        )
    rows.sort(key=lambda row: (row["destination"], row["source"], row["name"]))
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )


def _observed_target(
    document: dict[str, object],
) -> tuple[str, str, str, list[tuple[int, str, int, str]], str]:
    prefix = _compose_prefix(document)
    result = subprocess.run(
        [*prefix, "ps", "-q", SERVICE], check=True, text=True, capture_output=True
    )
    ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(ids) != 1:
        _fail("recovery did not leave exactly one target container")
    inspected = subprocess.run(
        [
            str(document["docker_bin"]),
            "inspect",
            ids[0],
            "--format",
            '{"container_id":{{json .Id}},"image_id":{{json .Image}},"image_reference":{{json .Config.Image}},"ports":{{json .NetworkSettings.Ports}},"mounts":{{json .Mounts}}}',
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
        "mounts",
    }:
        _fail("recovery target observation has unexpected fields")
    ports = observation["ports"]
    if not isinstance(ports, dict):
        _fail("recovery target ports are not an object")
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
        _volume_identity(observation["mounts"]),
    )


def _require_database_healthy(document: dict[str, object], container: str) -> None:
    """PostgreSQL accepting connections, with the declared standby streaming."""

    docker_bin = str(document["docker_bin"])
    deadline = time.monotonic() + 120
    while True:
        ready = subprocess.run(
            [docker_bin, "exec", container, "pg_isready", "-U", "postgres"],
            check=False,
            capture_output=True,
            text=True,
        )
        if ready.returncode == 0:
            streaming = subprocess.run(
                [
                    docker_bin,
                    "exec",
                    container,
                    "psql",
                    "-U",
                    "postgres",
                    "-tAc",
                    "select 1 from pg_stat_replication where state = 'streaming' "
                    f"and client_addr = '{REPLICATION_STANDBY}' limit 1",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if streaming.returncode == 0 and streaming.stdout.strip() == "1":
                return
        if time.monotonic() >= deadline:
            _fail(
                "after recovery PostgreSQL is not healthy with the declared "
                "standby streaming"
            )
        time.sleep(2)


def _receipt_path() -> Path:
    return STATE_ROOT / RECEIPT_NAME


def _write_forward_receipt(
    document: dict[str, object], after_container_id: str
) -> None:
    """The terminal record that stops this bootstrap being repeated.

    Forward recovery is a terminal outcome: the pin is retained AND the
    listener is already corrected, so there is nothing a second bootstrap
    could add except another unreviewed recreate.
    """

    path = _receipt_path()
    if path.exists():
        return
    operation = str(document["operation_id"])
    receipt = {
        "schema": "LegacyImagePinBootstrapForwardRecoveryReceiptV1",
        "outcome": "recovered_forward",
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


def _recover_forward(document: dict[str, object], reason: str) -> None:
    """Recreate FORWARD. Never restore the dual-family listener.

    Every assertion below is a success condition Michael named: the pinned
    image unchanged, data identity unchanged, exactly one IPv4 listener, NO
    IPv6 listener, PostgreSQL healthy with the standby streaming, and a
    container that really was recreated.
    """

    if document["state"] != "armed":
        return
    if reason not in TERMINAL_REASONS - {"verified-success"}:
        _fail("unsupported recovery reason")
    _ensure_forward_bind(document)
    subprocess.run(_recreate_command(document), check=True)
    expected = _listener_keys(document["forward_listeners"], "deadman forward target")
    after_id, image_id, reference, listeners, volumes = _observed_target(document)

    if image_id != document["before_image_id"]:
        _fail("the recovered container does not run the pinned image ID")
    if reference != document["retained_image_reference"]:
        _fail("the recovered container did not retain the immutable reference")
    if volumes != document["volume_identity_digest"]:
        _fail("the recovered container's data/volume identity changed")
    if any(family == 6 for family in (_family(row[1]) for row in listeners)):
        # The whole reason this operation exists. Its reappearance is a
        # failure, never a restored state.
        _fail(
            "an IPv6 listener is present after recovery; the dual-family publish "
            "is the vulnerability and may not be recreated automatically"
        )
    if listeners != expected:
        _fail("the recovered container is not bound to exactly the IPv4 target")
    _require_database_healthy(document, after_id)

    document["state"] = "recovered_forward"
    document["state_reason"] = reason
    document["updated_at"] = _now()
    _write_forward_receipt(document, after_id)


def _family(address: str) -> int:
    return ipaddress.ip_address(address).version


def check(operation: str) -> None:
    with _locked_state(operation) as (path, document):
        if document["state"] != "armed":
            return
        if datetime.now(UTC) < _parse_time(document["deadline"], "deadline"):
            return
        _recover_forward(document, "timeout")
        _atomic_write(path, document)


def recover_forward(operation: str, reason: str) -> None:
    with _locked_state(operation) as (path, document):
        _recover_forward(document, reason)
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
    rollback = commands.add_parser("recover-forward")
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
        elif args.command == "recover-forward":
            recover_forward(args.operation, args.reason)
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
