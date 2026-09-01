#!/usr/bin/python3 -I
"""Root-owned read-only observer for published-port PLAN v2.

The Actions planning identity must not hold the Docker socket or a general
sudo capability.  It may invoke only an installed copy of this program for one
of the root-owned config's allowed services.  Docker's effective configuration
can contain resolved secrets, so it is reduced in memory and never emitted;
stdout contains only the canonical safe snapshot contract.
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

CONFIG_PATH = Path("/etc/dotmac/published-port-plan-observer.json")
IMAGE_REFERENCE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
SERVICE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
CONFIG_FIELDS = {
    "schema",
    "target_server_name",
    "compose_project",
    "docker_bin",
    "deploy_dir",
    "env_file",
    "compose_files",
    "allowed_services",
}
SAFE_ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/root",
    "DOCKER_HOST": "unix:///var/run/docker.sock",
    "COMPOSE_ANSI": "never",
}


class ObserverError(RuntimeError):
    pass


def _fail(message: str) -> Never:
    raise ObserverError(message)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical(value)).hexdigest()}"


def _load_config(path: Path = CONFIG_PATH) -> dict[str, object]:
    stat = path.stat()
    if stat.st_uid != 0 or stat.st_mode & 0o022:
        _fail("observer config must be root-owned and not group/world writable")
    raw = path.read_bytes()
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ObserverError(f"observer config is invalid JSON: {error}") from error
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS:
        _fail("observer config fields differ from PublishedPortObserverConfigV1")
    if raw != _canonical(config):
        _fail("observer config is not canonical")
    if config["schema"] != "PublishedPortObserverConfigV1":
        _fail("unsupported observer config schema")
    if config["target_server_name"] != "dotmac-sub-prod":
        _fail("observer config names another target")
    if config["compose_project"] != "dotmac_sub":
        _fail("observer config names another Compose project")
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
    services = config["allowed_services"]
    if (
        not isinstance(services, list)
        or not services
        or services != sorted(set(services))
        or any(
            not isinstance(item, str) or not SERVICE.fullmatch(item)
            for item in services
        )
    ):
        _fail("observer allowed services must be unique, sorted service names")
    return config


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


def collect(service: str, *, config_path: Path = CONFIG_PATH) -> dict[str, object]:
    config = _load_config(config_path)
    if service not in config["allowed_services"]:
        _fail("service is not admitted by the root-owned observer config")
    compose = _compose_prefix(config)
    ids_result = subprocess.run(
        [*compose, "ps", "-q"],
        check=True,
        capture_output=True,
        text=True,
        env=SAFE_ENV,
    )
    ids = sorted(
        {line.strip() for line in ids_result.stdout.splitlines() if line.strip()}
    )
    if not ids:
        _fail("Compose project has no running containers")
    inspect = subprocess.run(
        [
            str(config["docker_bin"]),
            "inspect",
            *ids,
            "--format",
            '{"compose_project":{{json (index .Config.Labels "com.docker.compose.project")}},"service":{{json (index .Config.Labels "com.docker.compose.service")}},"container":{{json .Name}},"container_id":{{json .Id}},"image_id":{{json .Image}},"image_reference":{{json .Config.Image}},"ports":{{json .NetworkSettings.Ports}}}',
        ],
        check=True,
        capture_output=True,
        text=True,
        env=SAFE_ENV,
    )
    containers: list[dict[str, object]] = []
    allowed = {
        "compose_project",
        "service",
        "container",
        "container_id",
        "image_id",
        "image_reference",
        "ports",
    }
    for line in inspect.stdout.splitlines():
        row = json.loads(line)
        if not isinstance(row, dict) or set(row) != allowed:
            _fail("Docker inspect emitted an unexpected field set")
        if row["compose_project"] != config["compose_project"]:
            _fail("Docker inspect escaped the configured Compose project")
        row["container"] = str(row["container"]).lstrip("/")
        listeners: list[dict[str, object]] = []
        ports = row.pop("ports")
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
        containers.append(
            {
                "compose_project": row["compose_project"],
                "service": row["service"],
                "container": row["container"],
                "container_id": str(row["container_id"]).removeprefix("sha256:"),
                "image_id": row["image_id"],
                "image_reference": row["image_reference"],
                "listeners": listeners,
            }
        )
    containers.sort(
        key=lambda item: (item["service"], item["container"], item["container_id"])
    )
    if len(tuple(row for row in containers if row["service"] == service)) != 1:
        _fail("Docker observation must contain exactly one target container")

    effective = subprocess.run(
        [*compose, "config", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
        env=SAFE_ENV,
    )
    document = json.loads(effective.stdout)
    if not isinstance(document, dict) or not isinstance(document.get("services"), dict):
        _fail("effective Compose output has no services object")
    definition = document["services"].get(service)
    if not isinstance(definition, dict):
        _fail("effective Compose output has no target service")
    projection = dict(definition)
    projection.pop("ports", None)
    image = projection.get("image")
    if not isinstance(image, str) or not IMAGE_REFERENCE.fullmatch(image):
        _fail("target service image is not immutable and digest-pinned")
    return {
        "schema": "PublishedPortHostSnapshotV2",
        "target_server_name": config["target_server_name"],
        "service": service,
        "observer_digest": f"sha256:{hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}",
        "non_port_projection": "DockerComposeServiceProjectionV1",
        "non_port_definition_digest": _digest(projection),
        "effective_image_reference": image,
        "containers": containers,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect_command = commands.add_parser("collect")
    collect_command.add_argument("--service", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        document = collect(args.service)
    except (ObserverError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"PUBLISHED PORT OBSERVER FAILED: {error}", file=sys.stderr)
        return 1
    os.write(sys.stdout.fileno(), _canonical(document))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
