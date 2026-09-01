#!/usr/bin/python3 -I
"""Root-owned read-only observer for the one-time legacy image-pin bootstrap.

This is a SEPARATE program from the steady-state published-port observer, and
deliberately so.  The steady-state observer refuses a target that is not
already ``name@sha256:...`` pinned, and that refusal is not relaxed anywhere.
This program is the only place in the tree that may look at a tag-pinned
target, it emits a DIFFERENT schema that the steady-state planner cannot read,
and it refuses to run at all once the bootstrap has a terminal receipt.

The digest it selects comes from the RUNNING image's own registry digest and
is proved to resolve, locally and with no pull, to the running image ID.  It is
never obtained by asking a registry what the mutable tag means now: that could
name a newer image, and adopting it would silently schedule an upgrade inside a
containment change.  If the running bytes cannot be bound to a registry digest,
this program stops.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Never

CONFIG_PATH = Path("/etc/dotmac/legacy-image-pin-observer.json")
RECEIPT_PATH = Path("/var/lib/dotmac/legacy-image-pin/receipt.json")
SERVICE = "postgres-local"
LEGACY_TAG = re.compile(
    r"^[a-z0-9]+(?:[._\-/][a-z0-9]+)*:[A-Za-z0-9_][A-Za-z0-9._\-]{0,127}$"
)
DIGEST_REFERENCE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
CONFIG_FIELDS = {
    "schema",
    "target_server_name",
    "compose_project",
    "docker_bin",
    "deploy_dir",
    "env_file",
    "compose_files",
    "service",
    "legacy_image_reference",
}
SAFE_ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/root",
    "DOCKER_HOST": "unix:///var/run/docker.sock",
    "COMPOSE_ANSI": "never",
}


class BootstrapObserverError(RuntimeError):
    pass


def _fail(message: str) -> Never:
    raise BootstrapObserverError(message)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical(value)).hexdigest()}"


def _repository_of(reference: str) -> str:
    if "@" in reference:
        return reference.rsplit("@", 1)[0]
    return reference.rsplit(":", 1)[0]


def _load_config(path: Path = CONFIG_PATH) -> dict[str, object]:
    stat = path.stat()
    if stat.st_uid != 0 or stat.st_mode & 0o022:
        _fail("observer config must be root-owned and not group/world writable")
    raw = path.read_bytes()
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as error:
        raise BootstrapObserverError(
            f"observer config is invalid JSON: {error}"
        ) from error
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS:
        _fail("observer config fields differ from LegacyImagePinObserverConfigV1")
    if raw != _canonical(config):
        _fail("observer config is not canonical")
    if config["schema"] != "LegacyImagePinObserverConfigV1":
        _fail("unsupported observer config schema")
    if config["target_server_name"] != "dotmac-sub-prod":
        _fail("observer config names another target")
    if config["compose_project"] != "dotmac_sub":
        _fail("observer config names another Compose project")
    if config["service"] != SERVICE:
        _fail("the bootstrap observer serves only postgres-local")
    legacy = config["legacy_image_reference"]
    if not isinstance(legacy, str) or not LEGACY_TAG.fullmatch(legacy):
        _fail("observer config legacy image reference is not a mutable tag")
    for field in ("deploy_dir", "env_file"):
        if not isinstance(config[field], str) or not Path(config[field]).is_absolute():
            _fail(f"observer {field} must be absolute")
    docker_bin = config["docker_bin"]
    if not isinstance(docker_bin, str) or not Path(docker_bin).is_absolute():
        _fail("observer docker_bin must be absolute")
    docker_path = Path(docker_bin)
    docker_stat = docker_path.stat()
    if (
        not docker_path.is_file()
        or not os.access(docker_path, os.X_OK)
        or docker_stat.st_uid != 0
        or docker_stat.st_mode & 0o022
    ):
        _fail("observer docker_bin must be root-owned, executable and non-writable")
    compose_files = config["compose_files"]
    if not isinstance(compose_files, list) or not compose_files:
        _fail("observer config must name Compose files")
    if any(
        not isinstance(item, str) or not Path(item).is_absolute()
        for item in compose_files
    ):
        _fail("observer Compose paths must be absolute")
    return config


def _refuse_a_repeat(receipt_path: Path = RECEIPT_PATH) -> None:
    """The earliest of the single-use refusals.

    A terminal receipt means this bootstrap has already run -- applied or
    rolled back -- so there is nothing left for it to do and no second
    unreviewed recreate to authorize.
    """

    if receipt_path.exists():
        _fail(
            "a terminal legacy image-pin receipt already exists; this bootstrap "
            "is single-use and cannot run again"
        )


def _compose_prefix(config: dict[str, object]) -> list[str]:
    command = [
        str(config["docker_bin"]),
        "compose",
        "--project-name",
        str(config["compose_project"]),
        "--project-directory",
        str(config["deploy_dir"]),
        "--env-file",
        str(config["env_file"]),
    ]
    for path in config["compose_files"]:
        command.extend(("-f", str(path)))
    return command


def _run(command: list[str], overrides: dict[str, str] | None = None) -> str:
    environment = dict(SAFE_ENV)
    environment.update(overrides or {})
    result = subprocess.run(
        command, check=True, capture_output=True, text=True, env=environment
    )
    return result.stdout


def _target_publish(document: object) -> dict[str, object]:
    """The single declared publish for the target's 9001/tcp socket."""

    if not isinstance(document, dict) or not isinstance(document.get("services"), dict):
        _fail("effective Compose output has no services object")
    definition = document["services"].get(SERVICE)
    if not isinstance(definition, dict):
        _fail("effective Compose output has no target service")
    rows = [
        row
        for row in (definition.get("ports") or ())
        if isinstance(row, dict)
        and int(row.get("published") or 0) == 9001
        and int(row.get("target") or 0) == 5432
        and str(row.get("protocol") or "tcp") == "tcp"
    ]
    if len(rows) != 1:
        _fail("the target service must declare exactly one 9001/tcp publish")
    return rows[0]


def _prove_bind_knob(compose: list[str]) -> dict[str, object]:
    """Render effective Compose under two different bind values.

    ``docker compose config`` only reads and renders; it starts nothing. Two
    DIFFERENT injections are required because a file that hardcodes the
    wildcard would satisfy a single wildcard probe while being completely
    unresponsive to the variable.
    """

    proof: dict[str, object] = {
        "schema": "LegacyImagePinBindKnobProofV1",
        "env_key": "PG_LOCAL_BIND",
        "wildcard_injection": "0.0.0.0:",
        "control_injection": "127.0.0.1:",
        "host_port": 9001,
        "container_port": 5432,
        "protocol": "tcp",
    }
    for injection, field in (
        ("0.0.0.0:", "wildcard_host_ip"),
        ("127.0.0.1:", "control_host_ip"),
        (None, "current_host_ip"),
    ):
        rendered = json.loads(
            _run(
                [*compose, "config", "--format", "json"],
                {} if injection is None else {"PG_LOCAL_BIND": injection},
            )
        )
        publish = _target_publish(rendered)
        host_ip = str(publish.get("host_ip") or "")
        if not host_ip:
            if injection is None:
                _fail(
                    "the deployed Compose and .env resolve this publish to no "
                    "host address at all -- a bare publish, which is dual-family "
                    "and whose IPv6 half no DOCKER-USER rule can reach"
                )
            _fail(
                "setting PG_LOCAL_BIND produced a publish with no host address; "
                "the deployed Compose file does not interpolate this variable, "
                "so the bootstrap cannot correct the listener through it"
            )
        proof[field] = str(ipaddress.ip_address(host_ip))
    if proof["wildcard_host_ip"] == proof["control_host_ip"]:
        _fail("the bind variable does not move the published address")
    return proof


def _select_registry_digest(
    config: dict[str, object], image_id: str, legacy_reference: str
) -> str:
    """Bind the RUNNING bytes to a registry digest, or stop.

    The lookup is keyed by the running image ID, never by the tag.  Resolving
    the tag would ask the registry what it means NOW, which may be a newer
    image than the one running; adopting that would smuggle an upgrade into a
    containment change.
    """

    raw = _run(
        [
            str(config["docker_bin"]),
            "image",
            "inspect",
            image_id,
            "--format",
            "{{json .RepoDigests}}",
        ]
    )
    digests = json.loads(raw)
    if not isinstance(digests, list):
        _fail("Docker did not report a repository digest list")
    repository = _repository_of(legacy_reference)
    matching = sorted(
        {
            item
            for item in digests
            if isinstance(item, str)
            and DIGEST_REFERENCE.fullmatch(item)
            and _repository_of(item) == repository
        }
    )
    if not matching:
        _fail(
            "the running image has no registry digest for the legacy repository; "
            "the running bytes cannot be bound to an immutable reference, so the "
            "bootstrap stops rather than adopting the digest behind the tag"
        )
    if len(matching) > 1:
        _fail(
            "the running image reports more than one registry digest for the "
            "legacy repository; the immutable reference is ambiguous"
        )
    return matching[0]


def _prove_local_resolution(
    config: dict[str, object], reference: str, running_image_id: str
) -> dict[str, object]:
    """``docker image inspect`` the desired digest; it must be the running bytes.

    ``docker image inspect`` never pulls, so this is a local resolution: it
    answers "are the bytes this digest names already here, and are they the
    exact bytes the target is running?"
    """

    raw = _run(
        [
            str(config["docker_bin"]),
            "image",
            "inspect",
            reference,
            "--format",
            "{{json .Id}}",
        ]
    )
    resolved = json.loads(raw)
    if not isinstance(resolved, str) or not IMAGE_ID.fullmatch(resolved):
        _fail("the desired digest did not resolve to an image ID")
    if resolved != running_image_id:
        _fail(
            "the desired digest resolves to different bytes than the running "
            "container; the bootstrap stops rather than scheduling an upgrade"
        )
    return {
        "schema": "LegacyImagePinLocalResolutionV1",
        "reference": reference,
        "resolved_image_id": resolved,
        "running_image_id": running_image_id,
        "pulled": False,
    }


def collect(
    *, config_path: Path = CONFIG_PATH, receipt_path: Path = RECEIPT_PATH
) -> dict[str, object]:
    _refuse_a_repeat(receipt_path)
    config = _load_config(config_path)
    legacy_reference = str(config["legacy_image_reference"])
    compose = _compose_prefix(config)

    ids = sorted(
        {
            line.strip()
            for line in _run([*compose, "ps", "-q"]).splitlines()
            if line.strip()
        }
    )
    if not ids:
        _fail("Compose project has no running containers")
    inspected = _run(
        [
            str(config["docker_bin"]),
            "inspect",
            *ids,
            "--format",
            '{"compose_project":{{json (index .Config.Labels "com.docker.compose.project")}},"service":{{json (index .Config.Labels "com.docker.compose.service")}},"container":{{json .Name}},"container_id":{{json .Id}},"image_id":{{json .Image}},"image_reference":{{json .Config.Image}},"ports":{{json .NetworkSettings.Ports}}}',
        ]
    )
    allowed = {
        "compose_project",
        "service",
        "container",
        "container_id",
        "image_id",
        "image_reference",
        "ports",
    }
    target: dict[str, object] | None = None
    non_targets: list[dict[str, object]] = []
    for line in inspected.splitlines():
        row = json.loads(line)
        if not isinstance(row, dict) or set(row) != allowed:
            _fail("Docker inspect emitted an unexpected field set")
        if row["compose_project"] != config["compose_project"]:
            _fail("Docker inspect escaped the configured Compose project")
        container = str(row["container"]).lstrip("/")
        container_id = str(row["container_id"]).removeprefix("sha256:")
        if row["service"] != SERVICE:
            # Identity only. A non-target is never recreated here, so its own
            # image reference is not a property this operation can promise.
            non_targets.append(
                {
                    "service": row["service"],
                    "container": container,
                    "container_id": container_id,
                }
            )
            continue
        if target is not None:
            _fail("the project must contain exactly one target container")
        listeners: list[dict[str, object]] = []
        ports = row["ports"]
        if not isinstance(ports, dict):
            _fail("Docker inspect ports are not an object")
        for port_spec, bindings in ports.items():
            container_port, separator, protocol = str(port_spec).partition("/")
            if not separator or protocol not in {"tcp", "udp"}:
                _fail("Docker inspect emitted an unsupported port key")
            if bindings is not None and not isinstance(bindings, list):
                _fail("Docker inspect emitted invalid port bindings")
            for binding in bindings or ():
                if not isinstance(binding, dict) or set(binding) != {
                    "HostIp",
                    "HostPort",
                }:
                    _fail("Docker inspect emitted an invalid port binding")
                listeners.append(
                    {
                        "container_port": int(container_port),
                        "host_ip": str(ipaddress.ip_address(binding["HostIp"])),
                        "host_port": int(binding["HostPort"]),
                        "protocol": protocol,
                    }
                )
        listeners.sort(
            key=lambda item: (
                item["container_port"],
                item["host_ip"],
                item["host_port"],
                item["protocol"],
            )
        )
        target = {
            "container_id": container_id,
            "image_id": str(row["image_id"]),
            "image_reference": str(row["image_reference"]),
            "listeners": listeners,
        }
    if target is None:
        _fail("the project must contain exactly one target container")
    non_targets.sort(
        key=lambda item: (item["service"], item["container"], item["container_id"])
    )

    running_reference = str(target["image_reference"])
    if running_reference != legacy_reference:
        _fail("the running target does not carry the configured legacy tag")
    if not LEGACY_TAG.fullmatch(running_reference):
        _fail("the running target reference is not a mutable tag")
    running_image_id = str(target["image_id"])
    if not IMAGE_ID.fullmatch(running_image_id):
        _fail("the running target image ID is malformed")

    desired = _select_registry_digest(config, running_image_id, legacy_reference)
    resolution = _prove_local_resolution(config, desired, running_image_id)

    document = json.loads(_run([*compose, "config", "--format", "json"]))
    if not isinstance(document, dict) or not isinstance(document.get("services"), dict):
        _fail("effective Compose output has no services object")
    definition = document["services"].get(SERVICE)
    if not isinstance(definition, dict):
        _fail("effective Compose output has no target service")
    projection = dict(definition)
    projection.pop("ports", None)
    effective_image = projection.get("image")
    if not isinstance(effective_image, str) or effective_image != legacy_reference:
        _fail("effective Compose image is not the exact observed legacy tag")
    image_free = dict(projection)
    image_free.pop("image", None)

    bind_knob = _prove_bind_knob(compose)

    # CURRENT state: the bytes actually deployed on this host. The Actions
    # checkout is never allowed to stand in for this.
    deployed_compose_files = sorted(
        (
            {
                "path": str(path),
                "digest": (
                    "sha256:" + hashlib.sha256(Path(str(path)).read_bytes()).hexdigest()
                ),
            }
            for path in config["compose_files"]
        ),
        key=lambda row: row["path"],
    )

    return {
        "schema": "LegacyImagePinBootstrapSnapshotV1",
        "target_server_name": config["target_server_name"],
        "service": SERVICE,
        "observer_digest": (
            f"sha256:{hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}"
        ),
        "legacy_image_reference": legacy_reference,
        "desired_image_reference": desired,
        "resolution": resolution,
        "target_container_id": target["container_id"],
        "target_image_id": running_image_id,
        "listeners": target["listeners"],
        "non_port_projection": "DockerComposeServiceProjectionV1",
        "non_port_definition_digest": _digest(projection),
        "image_free_definition_digest": _digest(image_free),
        "effective_image_reference": effective_image,
        "deployed_compose_files": deployed_compose_files,
        "bind_knob": bind_knob,
        "non_targets": non_targets,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("collect")
    return parser


def main(argv: list[str] | None = None) -> int:
    _parser().parse_args(argv)
    try:
        document = collect()
    except (
        BootstrapObserverError,
        OSError,
        ValueError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"LEGACY IMAGE PIN OBSERVER FAILED: {error}", file=sys.stderr)
        return 1
    os.write(sys.stdout.fileno(), _canonical(document))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
