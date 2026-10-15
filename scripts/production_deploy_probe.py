"""Bounded Docker probes for a production writer cutover."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from urllib.parse import parse_qsl, urlsplit

_REPOSITORY: Final = "ghcr.io/michaelayoade/dotmac_sub"
_REVISION_LABEL: Final = "org.opencontainers.image.revision"
_APP_NAME: Final = "dotmac_sub_app"
_ID: Final = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION: Final = re.compile(r"^[0-9a-f]{40}$")
_HOST: Final = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,252}$")
_IPV4: Final = re.compile(r"^[0-9.]+$")
_PROBE_CODE: Final = (
    "import runpy,sys; sys.path.insert(0,'/app')\n"
    "module=sys.argv[1]; arguments=sys.argv[2:]\n"
    "if module=='alembic' and arguments[:1]==['heads']:\n"
    " from scripts.ci.migrated_test_database import install_migration_graph_environment\n"
    " install_migration_graph_environment()\n"
    "sys.argv=[module,*arguments]; runpy.run_module(module,run_name='__main__')"
)
_FLOOR_OPERATIONS: Final = {
    "payment-email-predecessor": ("scripts.verify_payment_email_rollback", ()),
    "payments-predecessor": ("scripts.verify_payments_intent_rollback", ()),
    "payments-supported": (
        "scripts.verify_payments_intent_rollback",
        ("inspect",),
    ),
    "payments-installed": (
        "scripts.verify_payments_intent_rollback",
        ("inspect",),
    ),
}
_OVERLAY_KEYS: Final = (
    "CELERY_BROKER_READ_URL",
    "CELERY_BROKER_WRITE_URL",
    "CELERY_BROKER_TRANSPORT_OPTIONS",
    "CELERY_CONFIG_MODULE",
)
_MIGRATION_GRAPH_KEYS: Final = (
    "DOTMAC_MIGRATION_BINDINGS",
    "DOTMAC_MODULE_PLANE_SELECTIONS",
)


def _migration_graph_environment() -> dict[str, str]:
    """Read Alembic's canonical, checked-in nonsecret graph pointers."""
    import ast

    source = (
        Path(__file__).resolve().parents[1] / "scripts/ci/migrated_test_database.py"
    )
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        assignment = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "_MIGRATION_GRAPH_ENVIRONMENT"
                for target in node.targets
            )
        )
        values = ast.literal_eval(assignment.value)
    except (OSError, SyntaxError, StopIteration, ValueError, TypeError):
        raise ProbeRefused("migration_graph_environment_unproved") from None
    if (
        type(values) is not dict
        or set(values) != set(_MIGRATION_GRAPH_KEYS)
        or any(
            type(key) is not str
            or type(value) is not str
            or not value.startswith("app.migration_bindings:")
            for key, value in values.items()
        )
    ):
        raise ProbeRefused("migration_graph_environment_unproved")
    return values


def _validate_payments_floor_output(operation: str, output: str) -> dict[str, object]:
    try:
        evidence = json.loads(output)
        observation = evidence["observation"]
        if (
            type(evidence) is not dict
            or evidence.get("schema_version") != 1
            or type(observation) is not dict
            or observation.get("legacy_shape_complete") is not True
        ):
            raise ValueError
        counts = tuple(
            observation.get(key)
            for key in (
                "correlation_columns",
                "correlation_constraints",
                "correlation_triggers",
                "correlation_functions",
            )
        )
        if any(type(value) is not int for value in counts):
            raise ValueError
        floor = evidence.get("floor")
        if operation == "payments-supported":
            if floor == "predecessor":
                if observation.get(
                    "installed_shape_complete"
                ) is not False or counts != (
                    0,
                    0,
                    0,
                    0,
                ):
                    raise ValueError
            elif floor == "installed":
                if observation.get(
                    "installed_shape_complete"
                ) is not True or counts != (2, 2, 1, 1):
                    raise ValueError
            else:
                raise ValueError
        elif operation == "payments-installed":
            if (
                floor != "installed"
                or observation.get("installed_shape_complete") is not True
                or counts != (2, 2, 1, 1)
            ):
                raise ValueError
        else:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ProbeRefused("payments_floor_unproved") from None
    return evidence


class ProbeRefused(ValueError):
    """A closed probe denial that never includes runtime configuration."""


class ProbeIndeterminate(ProbeRefused):
    """A Docker side effect timed out without terminal-state proof."""


@dataclass(frozen=True, slots=True)
class ExtraHostBinding:
    hostname: str
    address: str

    def __post_init__(self) -> None:
        if self.hostname != _canonical_host(
            self.hostname
        ) or self.address != _literal_ip(self.address):
            raise ProbeRefused("unsupported_name_resolution")


@dataclass(frozen=True, slots=True)
class EndpointBinding:
    container_id: str
    image_id: str
    started_at: str
    restart_count: int
    endpoint_id: str
    ipv4_address: str
    aliases: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            _ID.fullmatch(self.container_id) is None
            or _IMAGE_ID.fullmatch(self.image_id) is None
            or not self.started_at
            or type(self.restart_count) is not int
            or self.restart_count < 0
            or _ID.fullmatch(self.endpoint_id) is None
            or not _is_ipv4(self.ipv4_address)
            or not self.aliases
            or any(type(alias) is not str or not alias for alias in self.aliases)
        ):
            raise ProbeRefused("captured_binding_unproved")


@dataclass(frozen=True, slots=True)
class WriterBinding:
    service: str
    container_id: str
    image_id: str
    started_at: str
    restart_count: int
    endpoint_id: str
    ipv4_address: str
    aliases: tuple[str, ...]

    extra_hosts: tuple[ExtraHostBinding, ...] = ()

    def __post_init__(self) -> None:
        _validate_host_bindings(self.extra_hosts)
        if (
            not self.service
            or _ID.fullmatch(self.container_id) is None
            or _IMAGE_ID.fullmatch(self.image_id) is None
            or not self.started_at
            or type(self.restart_count) is not int
            or self.restart_count < 0
            or _ID.fullmatch(self.endpoint_id) is None
            or not _is_ipv4(self.ipv4_address)
            or not self.aliases
            or any(type(alias) is not str or not alias for alias in self.aliases)
        ):
            raise ProbeRefused("captured_binding_unproved")


@dataclass(frozen=True, slots=True)
class CapturedNetworkBinding:
    network_id: str
    network_name: str
    app_container_id: str
    app_image_id: str
    app_digest: str
    app_revision: str
    app_started_at: str
    app_restart_count: int
    candidate_image_id: str
    candidate_digest: str
    candidate_revision: str
    database_hostname: str
    database: EndpointBinding
    broker_hostname: str
    broker: EndpointBinding
    writers: tuple[WriterBinding, ...]
    inactive_services: tuple[str, ...]
    compose_arguments: tuple[str, ...]
    database_url: str = field(repr=False, compare=False)
    broker_url: str = field(repr=False, compare=False)
    broker_env_key: str = field(repr=False, compare=False)

    app_extra_hosts: tuple[ExtraHostBinding, ...] = ()

    def __post_init__(self) -> None:
        _validate_host_bindings(self.app_extra_hosts)
        if (
            _ID.fullmatch(self.network_id) is None
            or not self.network_name
            or _ID.fullmatch(self.app_container_id) is None
            or _IMAGE_ID.fullmatch(self.app_image_id) is None
            or _IMAGE_ID.fullmatch(self.app_digest) is None
            or _REVISION.fullmatch(self.app_revision) is None
            or not self.app_started_at
            or type(self.app_restart_count) is not int
            or self.app_restart_count < 0
            or _IMAGE_ID.fullmatch(self.candidate_image_id) is None
            or _IMAGE_ID.fullmatch(self.candidate_digest) is None
            or _REVISION.fullmatch(self.candidate_revision) is None
            or _HOST.fullmatch(self.database_hostname) is None
            or _HOST.fullmatch(self.broker_hostname) is None
            or len({item.service for item in self.writers}) != len(self.writers)
            or len({item.container_id for item in self.writers}) != len(self.writers)
            or len(set(self.inactive_services)) != len(self.inactive_services)
            or any(not service for service in self.inactive_services)
            or set(self.inactive_services) & {item.service for item in self.writers}
            or self.app_container_id
            in {self.database.container_id, self.broker.container_id}
            or any(
                item.container_id
                in {
                    self.app_container_id,
                    self.database.container_id,
                    self.broker.container_id,
                }
                for item in self.writers
            )
            or type(self.compose_arguments) is not tuple
            or any(
                type(value) is not str or not value for value in self.compose_arguments
            )
            or type(self.database_url) is not str
            or type(self.broker_url) is not str
            or type(self.broker_env_key) is not str
            or self.broker_env_key not in {"CELERY_BROKER_URL", "REDIS_URL"}
            or (
                self.database_url
                and _url_host(self.database_url, database=True)
                != self.database_hostname
            )
            or (
                self.broker_url
                and _url_host(self.broker_url, database=False) != self.broker_hostname
            )
        ):
            raise ProbeRefused("captured_binding_unproved")

    def public_json(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "network_id": self.network_id,
            "network_name": self.network_name,
            "app_container_id": self.app_container_id,
            "app_image_id": self.app_image_id,
            "app_digest": self.app_digest,
            "app_revision": self.app_revision,
            "app_started_at": self.app_started_at,
            "app_restart_count": self.app_restart_count,
            "candidate_image_id": self.candidate_image_id,
            "candidate_digest": self.candidate_digest,
            "candidate_revision": self.candidate_revision,
            "database_hostname": self.database_hostname,
            "database": _endpoint_json(self.database),
            "broker_hostname": self.broker_hostname,
            "broker": _endpoint_json(self.broker),
            "app_extra_hosts": _extra_hosts_json(self.app_extra_hosts),
            "writers": [
                {
                    "service": item.service,
                    "container_id": item.container_id,
                    "image_id": item.image_id,
                    "started_at": item.started_at,
                    "restart_count": item.restart_count,
                    "endpoint_id": item.endpoint_id,
                    "ipv4_address": item.ipv4_address,
                    "aliases": list(item.aliases),
                    "extra_hosts": _extra_hosts_json(item.extra_hosts),
                }
                for item in self.writers
            ],
            "inactive_services": list(self.inactive_services),
            "compose_arguments": list(self.compose_arguments),
        }


def _endpoint_json(value: EndpointBinding) -> dict[str, object]:
    return {
        "container_id": value.container_id,
        "image_id": value.image_id,
        "started_at": value.started_at,
        "restart_count": value.restart_count,
        "endpoint_id": value.endpoint_id,
        "ipv4_address": value.ipv4_address,
        "aliases": list(value.aliases),
    }


def _run(
    command: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 30,
    side_effecting: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        if side_effecting:
            raise ProbeIndeterminate("probe_side_effect_indeterminate") from None
        raise ProbeRefused("probe_command_unavailable") from None
    except OSError:
        raise ProbeRefused("probe_command_unavailable") from None


def _json(command: list[str]) -> object:
    result = _run(command)
    if result.returncode:
        raise ProbeRefused("probe_identity_unproved")
    try:
        return json.loads(result.stdout)
    except (TypeError, ValueError):
        raise ProbeRefused("probe_identity_unproved") from None


def _inspect(container_id: str) -> dict[str, object]:
    if _ID.fullmatch(container_id) is None:
        raise ProbeRefused("container_identity_unproved")
    result = _json(["docker", "inspect", "--format", "{{json .}}", container_id])
    if type(result) is not dict or result.get("Id") != container_id:
        raise ProbeRefused("container_identity_unproved")
    return result


def _inspect_named(name: str) -> dict[str, object]:
    if not name or "/" in name:
        raise ProbeRefused("container_identity_unproved")
    result = _json(["docker", "inspect", "--format", "{{json .}}", name])
    if type(result) is not dict or type(result.get("Id")) is not str:
        raise ProbeRefused("container_identity_unproved")
    return result


def _container_parts(
    container: dict[str, object],
) -> tuple[dict[str, str], dict[str, object], dict[str, object]]:
    config = container.get("Config")
    state = container.get("State")
    settings = container.get("NetworkSettings")
    if (
        type(config) is not dict
        or type(state) is not dict
        or type(settings) is not dict
    ):
        raise ProbeRefused("container_identity_unproved")
    raw_env = config.get("Env")
    if type(raw_env) is not list or any(type(value) is not str for value in raw_env):
        raise ProbeRefused("runtime_environment_unproved")
    environment: dict[str, str] = {}
    for value in raw_env:
        key, sep, item = value.partition("=")
        if not sep or key in environment:
            raise ProbeRefused("runtime_environment_unproved")
        environment[key] = item
    if any(environment.get(key) for key in _OVERLAY_KEYS):
        raise ProbeRefused("unsupported_broker_overlay")
    return environment, state, settings


def _service_urls(
    environment: dict[str, str], *, require_database: bool = False
) -> tuple[str, str, str]:
    database_url = environment.get("DATABASE_URL", "")
    celery_url = environment.get("CELERY_BROKER_URL", "")
    redis_url = environment.get("REDIS_URL", "")
    if celery_url and redis_url and celery_url != redis_url:
        raise ProbeRefused("unsupported_broker_overlay")
    broker_url = celery_url or redis_url
    broker_key = "CELERY_BROKER_URL" if celery_url else "REDIS_URL"
    if require_database and not database_url:
        raise ProbeRefused("database_binding_unproved")
    if not broker_url:
        raise ProbeRefused("broker_binding_unproved")
    if database_url:
        _url_host(database_url, database=True)
    _url_host(broker_url, database=False)
    return database_url, broker_url, broker_key


def _optional_service_urls(
    environment: dict[str, str],
) -> tuple[str, str, str]:
    database_url = environment.get("DATABASE_URL", "")
    celery_url = environment.get("CELERY_BROKER_URL", "")
    redis_url = environment.get("REDIS_URL", "")
    if celery_url and redis_url and celery_url != redis_url:
        raise ProbeRefused("unsupported_broker_overlay")
    broker_url = celery_url or redis_url
    broker_key = "CELERY_BROKER_URL" if celery_url else "REDIS_URL"
    if database_url:
        _url_host(database_url, database=True)
    if broker_url:
        _url_host(broker_url, database=False)
    return database_url, broker_url, broker_key


def _is_ipv4(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return True


def _url_host(value: str, *, database: bool) -> str:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except (TypeError, ValueError):
        raise ProbeRefused("runtime_endpoint_unproved") from None
    if (
        not value
        or "\0" in value
        or "\n" in value
        or "\r" in value
        or not hostname
        or _HOST.fullmatch(hostname) is None
        or _IPV4.fullmatch(hostname)
    ):
        raise ProbeRefused("runtime_endpoint_unproved")
    if database and parsed.scheme not in {
        "postgres",
        "postgresql",
        "postgresql+psycopg",
    }:
        raise ProbeRefused("runtime_endpoint_unproved")
    if database and any(
        key.casefold() in {"host", "hostaddr", "service"}
        for key, _item in parse_qsl(parsed.query, keep_blank_values=True)
    ):
        raise ProbeRefused("runtime_endpoint_unproved")
    if not database and (
        parsed.scheme != "redis"
        or not parsed.path.startswith("/")
        or not parsed.path[1:].isdigit()
        or parsed.query
        or parsed.fragment
    ):
        raise ProbeRefused("runtime_endpoint_unproved")
    return hostname


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _canonical_host(value: object) -> str:
    if type(value) is not str or not value.isascii():
        raise ProbeRefused("unsupported_name_resolution")
    host = value.lower().removesuffix(".")
    if (
        len(host) > 253
        or not host
        or any(
            re.fullmatch(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?", label) is None
            for label in host.split(".")
        )
    ):
        raise ProbeRefused("unsupported_name_resolution")
    return host


def _literal_ip(value: object) -> str:
    if type(value) is not str or "%" in value:
        raise ProbeRefused("unsupported_name_resolution")
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        raise ProbeRefused("unsupported_name_resolution") from None


def _extra_hosts(value: object) -> tuple[ExtraHostBinding, ...]:
    """Canonical Docker/Compose host mappings; no gateway or DNS resolution."""
    pairs: list[tuple[object, object]] = []
    if value is None:
        return ()
    if type(value) is dict:
        pairs = list(value.items())
    elif type(value) is list:
        for item in value:
            if type(item) is not str:
                raise ProbeRefused("unsupported_name_resolution")
            hostname, separator, address = item.partition("=" if "=" in item else ":")
            if not separator:
                raise ProbeRefused("unsupported_name_resolution")
            pairs.append((hostname, address))
    else:
        raise ProbeRefused("unsupported_name_resolution")
    result: dict[str, ExtraHostBinding] = {}
    for hostname, address in pairs:
        host = _canonical_host(hostname)
        if host in result:
            raise ProbeRefused("unsupported_name_resolution")
        result[host] = ExtraHostBinding(host, _literal_ip(address))
    return tuple(result[host] for host in sorted(result))


def _validate_host_bindings(value: tuple[ExtraHostBinding, ...]) -> None:
    if type(value) is not tuple or any(
        type(item) is not ExtraHostBinding for item in value
    ):
        raise ProbeRefused("captured_binding_unproved")
    if value != _extra_hosts(_extra_hosts_json(value)) or len(
        {item.hostname for item in value}
    ) != len(value):
        raise ProbeRefused("captured_binding_unproved")


def _extra_hosts_json(value: tuple[ExtraHostBinding, ...]) -> dict[str, str]:
    return {item.hostname: item.address for item in value}


def _require_default_name_resolution(
    container: dict[str, object],
) -> tuple[ExtraHostBinding, ...]:
    host_config = container.get("HostConfig")
    if type(host_config) is not dict or any(
        host_config.get(key) not in (None, [])
        for key in ("Dns", "DnsSearch", "DnsOptions", "Links")
    ):
        raise ProbeRefused("unsupported_name_resolution")
    return _extra_hosts(host_config.get("ExtraHosts"))


def _compose_extra_hosts(service: object) -> tuple[ExtraHostBinding, ...]:
    # Full rendered services/environment stay private process memory. Only this
    # canonical nonsecret projection can be retained in the binding artifact.
    if type(service) is not dict:
        raise ProbeRefused("compose_configuration_unproved")
    if any(
        service.get(key) not in (None, [], "")
        for key in ("dns", "dns_search", "dns_opt", "links", "external_links")
    ):
        raise ProbeRefused("unsupported_name_resolution")
    return _extra_hosts(service.get("extra_hosts"))


def _critical_endpoint_hosts(
    *,
    database_host: str,
    broker_host: str,
    database: EndpointBinding,
    broker: EndpointBinding,
    network_members: dict[str, object],
) -> frozenset[str]:
    names = {_canonical_host(database_host), _canonical_host(broker_host)}
    for expected in (database, broker):
        current = _inspect(expected.container_id)
        _env, _state, settings = _container_parts(current)
        name = current.get("Name")
        if type(name) is not str or not name.startswith("/") or not name[1:]:
            raise ProbeRefused("runtime_endpoint_unproved")
        names.add(_canonical_host(name[1:]))
        membership = network_members.get(expected.container_id)
        member_name = membership.get("Name") if type(membership) is dict else None
        if type(member_name) is not str or not member_name:
            raise ProbeRefused("runtime_endpoint_unproved")
        names.add(_canonical_host(member_name))
        networks = settings.get("Networks")
        if type(networks) is not dict or len(networks) != 1:
            raise ProbeRefused("unsupported_network_topology")
        endpoint = next(iter(networks.values()))
        if type(endpoint) is not dict:
            raise ProbeRefused("runtime_endpoint_unproved")
        for key in ("Aliases", "DNSNames"):
            aliases = endpoint.get(key)
            if aliases is None and key == "DNSNames":
                continue  # Older supported Docker inspect does not expose DNSNames.
            if type(aliases) is not list or any(
                type(alias) is not str for alias in aliases
            ):
                raise ProbeRefused("runtime_endpoint_unproved")
            names.update(_canonical_host(alias) for alias in aliases)
        names.update(_canonical_host(alias) for alias in expected.aliases)
    return frozenset(names)


def _attest_extra_hosts(
    actual: tuple[ExtraHostBinding, ...],
    captured: tuple[ExtraHostBinding, ...],
    target: tuple[ExtraHostBinding, ...],
    *,
    critical_hosts: frozenset[str],
) -> None:
    if any(
        item.hostname in critical_hosts
        for mapping in (actual, captured, target)
        for item in mapping
    ):
        raise ProbeRefused("critical_name_override")
    if actual != captured or captured != target:
        raise ProbeRefused("extra_hosts_binding_changed")


def _one_network(
    container: dict[str, object],
) -> tuple[str, str, dict[str, object]]:
    _env, _state, settings = _container_parts(container)
    networks = settings.get("Networks")
    if type(networks) is not dict or len(networks) != 1:
        raise ProbeRefused("unsupported_network_topology")
    name, endpoint = next(iter(networks.items()))
    if type(name) is not str or type(endpoint) is not dict:
        raise ProbeRefused("unsupported_network_topology")
    network_id = endpoint.get("NetworkID")
    if type(network_id) is not str or _ID.fullmatch(network_id) is None:
        raise ProbeRefused("unsupported_network_topology")
    endpoint_id = endpoint.get("EndpointID")
    address = endpoint.get("IPAddress")
    if (
        type(endpoint_id) is not str
        or _ID.fullmatch(endpoint_id) is None
        or not _is_ipv4(address)
    ):
        raise ProbeRefused("unsupported_network_topology")
    return network_id, name, endpoint


def _network(network_id: str) -> tuple[str, dict[str, object]]:
    result = _json(["docker", "network", "inspect", network_id])
    if (
        type(result) is not list
        or len(result) != 1
        or type(result[0]) is not dict
        or result[0].get("Id") != network_id
        or result[0].get("Scope") != "local"
        or type(result[0].get("Name")) is not str
        or type(result[0].get("Containers")) is not dict
    ):
        raise ProbeRefused("network_identity_unproved")
    return result[0]["Name"], result[0]["Containers"]


def _endpoint(hostname: str, *, network_id: str, network_name: str) -> EndpointBinding:
    actual_name, members = _network(network_id)
    if actual_name != network_name:
        raise ProbeRefused("network_identity_unproved")
    matches: list[EndpointBinding] = []
    for container_id, membership in members.items():
        if (
            type(container_id) is not str
            or _ID.fullmatch(container_id) is None
            or type(membership) is not dict
        ):
            raise ProbeRefused("network_identity_unproved")
        container = _inspect(container_id)
        _environment, state, settings = _container_parts(container)
        if state.get("Running") is not True:
            continue
        networks = settings.get("Networks")
        if type(networks) is not dict or set(networks) != {network_name}:
            raise ProbeRefused("unsupported_network_topology")
        record = networks[network_name]
        if type(record) is not dict or record.get("NetworkID") != network_id:
            raise ProbeRefused("network_identity_unproved")
        aliases = record.get("Aliases")
        endpoint_id = record.get("EndpointID")
        address = record.get("IPAddress")
        image_id = container.get("Image")
        started_at = state.get("StartedAt")
        restarts = container.get("RestartCount")
        if (
            type(aliases) is not list
            or any(type(alias) is not str for alias in aliases)
            or type(endpoint_id) is not str
            or _ID.fullmatch(endpoint_id) is None
            or not _is_ipv4(address)
            or type(image_id) is not str
            or _IMAGE_ID.fullmatch(image_id) is None
            or type(started_at) is not str
            or type(restarts) is not int
            or membership.get("EndpointID") != endpoint_id
            or str(membership.get("IPv4Address", "")).partition("/")[0] != address
        ):
            raise ProbeRefused("network_identity_unproved")
        if hostname in aliases:
            matches.append(
                EndpointBinding(
                    container_id,
                    image_id,
                    started_at,
                    restarts,
                    endpoint_id,
                    address,
                    tuple(aliases),
                )
            )
    if len(matches) != 1:
        raise ProbeRefused("runtime_endpoint_unproved")
    return matches[0]


def _candidate(reference: str, revision: str) -> tuple[str, str, dict[str, str]]:
    digest = reference.rsplit("@", maxsplit=1)[-1]
    if _IMAGE_ID.fullmatch(digest) is None or _REVISION.fullmatch(revision) is None:
        raise ProbeRefused("candidate_identity_unproved")
    image = _json(["docker", "image", "inspect", "--format", "{{json .}}", reference])
    config = image.get("Config") if type(image) is dict else None
    labels = config.get("Labels") if type(config) is dict else None
    if (
        type(image) is not dict
        or type(image.get("Id")) is not str
        or _IMAGE_ID.fullmatch(image["Id"]) is None
        or type(image.get("RepoDigests")) is not list
        or reference not in image["RepoDigests"]
        or type(labels) is not dict
        or labels.get(_REVISION_LABEL) != revision
    ):
        raise ProbeRefused("candidate_identity_unproved")
    raw_environment = config.get("Env")
    if type(raw_environment) is not list or any(
        type(value) is not str for value in raw_environment
    ):
        raise ProbeRefused("candidate_identity_unproved")
    environment: dict[str, str] = {}
    for value in raw_environment:
        key, separator, item = value.partition("=")
        if not separator or key in environment:
            raise ProbeRefused("candidate_identity_unproved")
        environment[key] = item
    return image["Id"], digest, environment


def _compose_environment(service: object) -> dict[str, str]:
    if type(service) is not dict:
        raise ProbeRefused("compose_configuration_unproved")
    environment = service.get("environment", {})
    if type(environment) is dict:
        if any(
            type(key) is not str or type(value) is not str
            for key, value in environment.items()
        ):
            raise ProbeRefused("compose_configuration_unproved")
        return dict(environment)
    if type(environment) is list:
        values: dict[str, str] = {}
        for entry in environment:
            if type(entry) is not str:
                raise ProbeRefused("compose_configuration_unproved")
            key, separator, value = entry.partition("=")
            if not separator or key in values:
                raise ProbeRefused("compose_configuration_unproved")
            values[key] = value
        return values
    raise ProbeRefused("compose_configuration_unproved")


def _validate_compose_arguments(arguments: tuple[str, ...]) -> None:
    """Accept only deploy.sh's fixed Compose source profile.

    The serialized binding is an observation artifact, so it may carry paths
    but must never become a way to inject arbitrary Compose flags, env files,
    plugins, or project overrides into later probes.
    """
    if len(arguments) not in {6, 8}:
        raise ProbeRefused("compose_configuration_unproved")
    if arguments[0] != "--project-directory" or arguments[2] != "--env-file":
        raise ProbeRefused("compose_configuration_unproved")
    if arguments[4] != "-f" or (len(arguments) == 8 and arguments[6] != "-f"):
        raise ProbeRefused("compose_configuration_unproved")
    project_directory = os.path.realpath(arguments[1])
    env_file = os.path.realpath(arguments[3])
    release_file = os.path.realpath(arguments[5])
    trusted_release_file = os.path.realpath(
        Path(__file__).resolve().parents[1] / "docker-compose.yml"
    )
    if (
        not os.path.isabs(arguments[1])
        or not os.path.isabs(arguments[3])
        or not os.path.isabs(arguments[5])
        or env_file != os.path.join(project_directory, ".env")
        or release_file != trusted_release_file
        or not os.path.isfile(env_file)
        or not os.path.isfile(release_file)
    ):
        raise ProbeRefused("compose_configuration_unproved")
    if len(arguments) == 8:
        override_file = os.path.realpath(arguments[7])
        if (
            not os.path.isabs(arguments[7])
            or override_file
            != os.path.join(project_directory, "docker-compose.override.yml")
            or not os.path.isfile(override_file)
        ):
            raise ProbeRefused("compose_configuration_unproved")


def _compose_services(
    arguments: tuple[str, ...], *, image_reference: str, revision: str
) -> dict[str, object]:
    _validate_compose_arguments(arguments)
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["APP_IMAGE"] = image_reference
    environment["GIT_SHA"] = revision
    try:
        result = subprocess.run(
            ["docker", "compose", *arguments, "config", "--format", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ProbeRefused("compose_configuration_unproved") from None
    if result.returncode:
        raise ProbeRefused("compose_configuration_unproved")
    try:
        value = json.loads(result.stdout, object_pairs_hook=_unique_json_object)
    except (TypeError, ValueError):
        raise ProbeRefused("compose_configuration_unproved") from None
    services = value.get("services") if type(value) is dict else None
    if type(services) is not dict:
        raise ProbeRefused("compose_configuration_unproved")
    return services


def _capture(
    *,
    app_container: str,
    writer_specs: tuple[str, ...],
    candidate_reference: str,
    candidate_revision: str,
    inactive_services: tuple[str, ...],
    compose_arguments: tuple[str, ...],
) -> CapturedNetworkBinding:
    app = _inspect(app_container)
    app_extra_hosts = _require_default_name_resolution(app)
    app_env, app_state, _settings = _container_parts(app)
    if app.get("Name") != f"/{_APP_NAME}" or app_state.get("Running") is not True:
        raise ProbeRefused("app_identity_unproved")
    database_url, broker_url, broker_key = _service_urls(app_env, require_database=True)
    database_host = _url_host(database_url, database=True)
    broker_host = _url_host(broker_url, database=False)
    network_id, network_name, _endpoint_config = _one_network(app)
    actual_network_name, _members = _network(network_id)
    if actual_network_name != network_name:
        raise ProbeRefused("network_identity_unproved")
    database = _endpoint(
        database_host, network_id=network_id, network_name=network_name
    )
    broker = _endpoint(broker_host, network_id=network_id, network_name=network_name)
    if app_container in {database.container_id, broker.container_id}:
        raise ProbeRefused("runtime_endpoint_unproved")

    writers: list[WriterBinding] = []
    for spec in writer_specs:
        service, separator, container_id = spec.partition("=")
        if not separator or not service or _ID.fullmatch(container_id) is None:
            raise ProbeRefused("writer_identity_unproved")
        container = _inspect(container_id)
        writer_extra_hosts = _require_default_name_resolution(container)
        writer_env, state, _settings = _container_parts(container)
        writer_database, writer_broker, _key = _optional_service_urls(writer_env)
        writer_network, writer_network_name, endpoint = _one_network(container)
        if (
            state.get("Running") is not True
            or (writer_broker and writer_broker != broker_url)
            or (writer_database and writer_database != database_url)
            or writer_network != network_id
            or writer_network_name != network_name
            or endpoint.get("NetworkID") != network_id
        ):
            raise ProbeRefused("writer_configuration_unproved")
        image_id = container.get("Image")
        started_at = state.get("StartedAt")
        restarts = container.get("RestartCount")
        endpoint_id = endpoint.get("EndpointID")
        address = endpoint.get("IPAddress")
        aliases = endpoint.get("Aliases")
        if (
            type(image_id) is not str
            or _IMAGE_ID.fullmatch(image_id) is None
            or type(started_at) is not str
            or type(restarts) is not int
            or type(endpoint_id) is not str
            or _ID.fullmatch(endpoint_id) is None
            or not _is_ipv4(address)
            or type(aliases) is not list
            or any(type(alias) is not str for alias in aliases)
        ):
            raise ProbeRefused("writer_identity_unproved")
        writers.append(
            WriterBinding(
                service,
                container_id,
                image_id,
                started_at,
                restarts,
                endpoint_id,
                address,
                tuple(aliases),
                writer_extra_hosts,
            )
        )
    if len({item.service for item in writers}) != len(writers) or len(
        {item.container_id for item in writers}
    ) != len(writers):
        raise ProbeRefused("writer_identity_unproved")
    candidate_id, candidate_digest, candidate_defaults = _candidate(
        candidate_reference, candidate_revision
    )
    services = _compose_services(
        compose_arguments,
        image_reference=candidate_reference,
        revision=candidate_revision,
    )
    critical_hosts = _critical_endpoint_hosts(
        database_host=database_host,
        broker_host=broker_host,
        database=database,
        broker=broker,
        network_members=_members,
    )
    _attest_extra_hosts(
        app_extra_hosts,
        app_extra_hosts,
        _compose_extra_hosts(services.get("app")),
        critical_hosts=critical_hosts,
    )
    app_target = candidate_defaults | _compose_environment(services.get("app"))
    for key in ("DATABASE_URL", "CELERY_BROKER_URL", "REDIS_URL", *_OVERLAY_KEYS):
        if app_env.get(key, "") != app_target.get(key, ""):
            raise ProbeRefused("candidate_configuration_mismatch")
    for service in inactive_services:
        if service not in services or service in {item.service for item in writers}:
            raise ProbeRefused("compose_configuration_unproved")
        target = candidate_defaults | _compose_environment(services[service])
        for key in ("DATABASE_URL", "CELERY_BROKER_URL", "REDIS_URL", *_OVERLAY_KEYS):
            if target.get(key, "") != app_target.get(key, ""):
                raise ProbeRefused("candidate_configuration_mismatch")
    for writer in writers:
        if writer.service not in services:
            raise ProbeRefused("compose_configuration_unproved")
        runtime_env, _state, _settings = _container_parts(_inspect(writer.container_id))
        writer_database, writer_broker, _broker_key = _optional_service_urls(
            runtime_env
        )
        if writer_database and writer_database != database_url:
            raise ProbeRefused("writer_configuration_unproved")
        if writer_broker and writer_broker != broker_url:
            raise ProbeRefused("writer_configuration_unproved")
        _attest_extra_hosts(
            writer.extra_hosts,
            writer.extra_hosts,
            _compose_extra_hosts(services[writer.service]),
            critical_hosts=critical_hosts,
        )
        target_env = candidate_defaults | _compose_environment(services[writer.service])
        for key in (
            "DATABASE_URL",
            "CELERY_BROKER_URL",
            "REDIS_URL",
            *_OVERLAY_KEYS,
        ):
            if runtime_env.get(key, "") != target_env.get(key, ""):
                raise ProbeRefused("candidate_configuration_mismatch")
    config = app.get("Config")
    labels = config.get("Labels") if type(config) is dict else None
    revision = labels.get(_REVISION_LABEL) if type(labels) is dict else None
    image_ref = config.get("Image") if type(config) is dict else None
    image_id = app.get("Image")
    if (
        type(revision) is not str
        or _REVISION.fullmatch(revision) is None
        or type(image_ref) is not str
        or not image_ref.startswith(f"{_REPOSITORY}@sha256:")
        or type(image_id) is not str
        or _IMAGE_ID.fullmatch(image_id) is None
        or type(app_state.get("StartedAt")) is not str
        or type(app.get("RestartCount")) is not int
    ):
        raise ProbeRefused("app_identity_unproved")
    digest = image_ref.removeprefix(f"{_REPOSITORY}@")
    if _IMAGE_ID.fullmatch(digest) is None:
        raise ProbeRefused("app_identity_unproved")
    binding = CapturedNetworkBinding(
        network_id,
        network_name,
        app_container,
        image_id,
        digest,
        revision,
        app_state["StartedAt"],
        app["RestartCount"],
        candidate_id,
        candidate_digest,
        candidate_revision,
        database_host,
        database,
        broker_host,
        broker,
        tuple(writers),
        inactive_services,
        compose_arguments,
        database_url,
        broker_url,
        broker_key,
        app_extra_hosts,
    )
    _verify(binding, allow_stopped_app=False)
    return binding


def _endpoint_from_json(value: object) -> EndpointBinding:
    if type(value) is not dict or type(value.get("aliases")) is not list:
        raise ProbeRefused("captured_binding_unproved")
    try:
        aliases = tuple(value["aliases"])
        if any(type(item) is not str for item in aliases):
            raise ValueError
        return EndpointBinding(
            value["container_id"],
            value["image_id"],
            value["started_at"],
            value["restart_count"],
            value["endpoint_id"],
            value["ipv4_address"],
            aliases,
        )
    except (KeyError, TypeError, ValueError):
        raise ProbeRefused("captured_binding_unproved") from None


def _binding_from_json(source: str) -> CapturedNetworkBinding:
    try:
        value = json.loads(source, object_pairs_hook=_unique_json_object)
        if type(value) is not dict or value.get("schema_version") != 1:
            raise ValueError
        raw_writers = value["writers"]
        if type(raw_writers) is not list:
            raise ValueError
        writers_list: list[WriterBinding] = []
        for entry in raw_writers:
            if type(entry) is not dict:
                raise ValueError
            fields = dict(entry)
            if type(fields.get("extra_hosts", {})) is not dict:
                raise ValueError
            fields["extra_hosts"] = _extra_hosts(fields.get("extra_hosts", {}))
            writers_list.append(WriterBinding(**fields))
        writers = tuple(writers_list)
        raw_inactive_services = value["inactive_services"]
        if type(raw_inactive_services) is not list or any(
            type(item) is not str or not item for item in raw_inactive_services
        ):
            raise ValueError
        raw_compose_arguments = value["compose_arguments"]
        if type(raw_compose_arguments) is not list or any(
            type(item) is not str or not item for item in raw_compose_arguments
        ):
            raise ValueError
        if type(value.get("app_extra_hosts", {})) is not dict:
            raise ValueError
        return CapturedNetworkBinding(
            value["network_id"],
            value["network_name"],
            value["app_container_id"],
            value["app_image_id"],
            value["app_digest"],
            value["app_revision"],
            value["app_started_at"],
            value["app_restart_count"],
            value["candidate_image_id"],
            value["candidate_digest"],
            value["candidate_revision"],
            value["database_hostname"],
            _endpoint_from_json(value["database"]),
            value["broker_hostname"],
            _endpoint_from_json(value["broker"]),
            writers,
            tuple(raw_inactive_services),
            tuple(raw_compose_arguments),
            "",
            "",
            "CELERY_BROKER_URL",
            _extra_hosts(value.get("app_extra_hosts", {})),
        )
    except (KeyError, TypeError, ValueError):
        raise ProbeRefused("captured_binding_unproved") from None


def _verify(
    binding: CapturedNetworkBinding, *, allow_stopped_app: bool
) -> tuple[str, str, str]:
    network_name, members = _network(binding.network_id)
    if network_name != binding.network_name:
        raise ProbeRefused("network_identity_changed")
    app = _inspect(binding.app_container_id)
    named_app = _inspect_named(_APP_NAME)
    if named_app.get("Id") != binding.app_container_id:
        raise ProbeRefused("app_identity_changed")
    environment, state, _settings = _container_parts(app)
    app_extra_hosts = _require_default_name_resolution(app)
    if app.get("Name") != f"/{_APP_NAME}":
        raise ProbeRefused("app_identity_changed")
    if allow_stopped_app:
        if state.get("Running") is True and state.get("Status") != "running":
            raise ProbeRefused("app_identity_changed")
        if state.get("Running") is False and state.get("Status") != "exited":
            raise ProbeRefused("app_identity_changed")
    elif state.get("Running") is not True or state.get("Status") != "running":
        raise ProbeRefused("app_identity_changed")
    if (
        app.get("Image") != binding.app_image_id
        or app.get("RestartCount") != binding.app_restart_count
        or state.get("StartedAt") != binding.app_started_at
    ):
        raise ProbeRefused("app_identity_changed")
    if state.get("Running") is True:
        network_id, actual_name, _app_endpoint = _one_network(app)
        if network_id != binding.network_id or actual_name != binding.network_name:
            raise ProbeRefused("app_network_changed")
    else:
        networks = _settings.get("Networks")
        endpoint = (
            networks.get(binding.network_name) if type(networks) is dict else None
        )
        if (
            type(networks) is not dict
            or set(networks) != {binding.network_name}
            or type(endpoint) is not dict
            or endpoint.get("NetworkID") != binding.network_id
        ):
            raise ProbeRefused("app_network_changed")
    database_url, broker_url, broker_key = _service_urls(
        environment, require_database=True
    )
    if binding.database_url and (database_url, broker_url, broker_key) != (
        binding.database_url,
        binding.broker_url,
        binding.broker_env_key,
    ):
        raise ProbeRefused("runtime_configuration_changed")
    config = app.get("Config")
    labels = config.get("Labels") if type(config) is dict else None
    image_reference = config.get("Image") if type(config) is dict else None
    if (
        type(labels) is not dict
        or labels.get(_REVISION_LABEL) != binding.app_revision
        or type(image_reference) is not str
        or image_reference != f"{_REPOSITORY}@{binding.app_digest}"
    ):
        raise ProbeRefused("app_identity_changed")
    expected_candidate_ref = f"{_REPOSITORY}@{binding.candidate_digest}"
    candidate_id, candidate_digest, candidate_defaults = _candidate(
        expected_candidate_ref, binding.candidate_revision
    )
    if (
        candidate_id != binding.candidate_image_id
        or candidate_digest != binding.candidate_digest
    ):
        raise ProbeRefused("candidate_identity_changed")
    services = _compose_services(
        binding.compose_arguments,
        image_reference=expected_candidate_ref,
        revision=binding.candidate_revision,
    )
    critical_hosts = _critical_endpoint_hosts(
        database_host=_url_host(database_url, database=True),
        broker_host=_url_host(broker_url, database=False),
        database=binding.database,
        broker=binding.broker,
        network_members=members,
    )
    _attest_extra_hosts(
        app_extra_hosts,
        binding.app_extra_hosts,
        _compose_extra_hosts(services.get("app")),
        critical_hosts=critical_hosts,
    )
    relevant_keys = ("DATABASE_URL", "CELERY_BROKER_URL", "REDIS_URL", *_OVERLAY_KEYS)
    app_target = candidate_defaults | _compose_environment(services.get("app"))
    if any(
        environment.get(key, "") != app_target.get(key, "") for key in relevant_keys
    ):
        raise ProbeRefused("candidate_configuration_changed")
    for service in binding.inactive_services:
        if service not in services:
            raise ProbeRefused("compose_configuration_unproved")
        target = candidate_defaults | _compose_environment(services[service])
        if any(target.get(key, "") != app_target.get(key, "") for key in relevant_keys):
            raise ProbeRefused("candidate_configuration_changed")
    for expected in (binding.database, binding.broker):
        current = _inspect(expected.container_id)
        _env, endpoint_state, settings = _container_parts(current)
        membership = members.get(expected.container_id)
        networks = settings.get("Networks")
        if (
            type(membership) is not dict
            or type(networks) is not dict
            or set(networks) != {binding.network_name}
        ):
            raise ProbeRefused("endpoint_network_changed")
        endpoint = networks[binding.network_name]
        if (
            endpoint_state.get("Running") is not True
            or current.get("Image") != expected.image_id
            or current.get("RestartCount") != expected.restart_count
            or endpoint_state.get("StartedAt") != expected.started_at
            or type(endpoint) is not dict
            or endpoint.get("NetworkID") != binding.network_id
            or endpoint.get("EndpointID") != expected.endpoint_id
            or endpoint.get("IPAddress") != expected.ipv4_address
            or endpoint.get("Aliases") != list(expected.aliases)
            or membership.get("EndpointID") != expected.endpoint_id
            or str(membership.get("IPv4Address", "")).partition("/")[0]
            != expected.ipv4_address
            or not str(membership.get("IPv4Address", "")).startswith(
                expected.ipv4_address + "/"
            )
        ):
            raise ProbeRefused("endpoint_identity_changed")
    for expected in binding.writers:
        current = _inspect(expected.container_id)
        writer_extra_hosts = _require_default_name_resolution(current)
        writer_env, writer_state, settings = _container_parts(current)
        writer_database, writer_broker, _writer_key = _optional_service_urls(writer_env)
        membership = members.get(expected.container_id)
        networks = settings.get("Networks")
        if (
            (writer_database and writer_database != database_url)
            or (writer_broker and writer_broker != broker_url)
            or type(networks) is not dict
        ):
            raise ProbeRefused("writer_configuration_changed")
        if expected.service not in services:
            raise ProbeRefused("compose_configuration_unproved")
        _attest_extra_hosts(
            writer_extra_hosts,
            expected.extra_hosts,
            _compose_extra_hosts(services[expected.service]),
            critical_hosts=critical_hosts,
        )
        writer_target = candidate_defaults | _compose_environment(
            services[expected.service]
        )
        if any(
            writer_env.get(key, "") != writer_target.get(key, "")
            for key in relevant_keys
        ):
            raise ProbeRefused("candidate_configuration_changed")
        running = writer_state.get("Running") is True
        endpoint = networks.get(binding.network_name)
        if (
            current.get("Image") != expected.image_id
            or current.get("RestartCount") != expected.restart_count
            or writer_state.get("StartedAt") != expected.started_at
            or (not running and writer_state.get("Status") != "exited")
            or (
                running
                and (
                    type(membership) is not dict
                    or set(networks) != {binding.network_name}
                    or type(endpoint) is not dict
                    or endpoint.get("NetworkID") != binding.network_id
                    or endpoint.get("EndpointID") != expected.endpoint_id
                    or endpoint.get("IPAddress") != expected.ipv4_address
                    or endpoint.get("Aliases") != list(expected.aliases)
                    or membership.get("EndpointID") != expected.endpoint_id
                    or str(membership.get("IPv4Address", "")).partition("/")[0]
                    != expected.ipv4_address
                )
            )
        ):
            raise ProbeRefused("writer_identity_changed")
    return database_url, broker_url, broker_key


def verify_fixed_compose_inventory(
    binding: CapturedNetworkBinding, *, known_terminal_ids: tuple[str, ...] = ()
) -> None:
    """Require every effective app-image service to retain its captured identity."""
    reference = f"{_REPOSITORY}@{binding.candidate_digest}"
    services = _compose_services(
        binding.compose_arguments,
        image_reference=reference,
        revision=binding.candidate_revision,
    )
    expected = {"app": binding.app_container_id}
    expected.update({writer.service: writer.container_id for writer in binding.writers})
    inactive = set(binding.inactive_services)
    if len(inactive) != len(binding.inactive_services) or inactive & set(expected):
        raise ProbeRefused("compose_inventory_unproved")
    app_image_services = {
        name
        for name, service in services.items()
        if type(service) is dict
        and type(service.get("image")) is str
        and (
            service["image"] == _REPOSITORY
            or service["image"].startswith(f"{_REPOSITORY}:")
            or service["image"].startswith(f"{_REPOSITORY}@")
        )
    }
    if app_image_services != set(expected) | inactive:
        raise ProbeRefused("compose_inventory_unproved")
    if any(services[name].get("image") != reference for name in app_image_services):
        raise ProbeRefused("compose_inventory_changed")

    for service in sorted(app_image_services):
        result = _run(
            [
                "docker",
                "compose",
                *binding.compose_arguments,
                "ps",
                "-a",
                "-q",
                service,
            ]
        )
        if result.returncode != 0:
            raise ProbeRefused("compose_inventory_unproved")
        identifiers = result.stdout.splitlines()
        if service in inactive:
            if identifiers:
                raise ProbeRefused("compose_inventory_changed")
            continue
        if identifiers != [expected[service]]:
            raise ProbeRefused("compose_inventory_changed")

    all_containers = _run(["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}"])
    if all_containers.returncode != 0:
        raise ProbeRefused("compose_inventory_unproved")
    identifiers = all_containers.stdout.splitlines()
    if any(_ID.fullmatch(identifier) is None for identifier in identifiers) or len(
        identifiers
    ) != len(set(identifiers)):
        raise ProbeRefused("compose_inventory_changed")
    if any(_ID.fullmatch(identifier) is None for identifier in known_terminal_ids):
        raise ProbeRefused("compose_inventory_unproved")
    expected_ids = set(expected.values()) | set(known_terminal_ids)
    recognized_image_ids = {
        binding.app_image_id,
        binding.candidate_image_id,
        *(writer.image_id for writer in binding.writers),
    }
    repository_images: dict[str, tuple[str, str]] = {}
    repo_digests_by_image: dict[str, tuple[str, ...]] = {}
    for identifier in identifiers:
        value = _json(["docker", "inspect", "--format", "{{json .}}", identifier])
        if type(value) is not dict or value.get("Id") != identifier:
            raise ProbeRefused("compose_inventory_unproved")
        config = value.get("Config")
        image_reference = config.get("Image") if type(config) is dict else None
        if type(image_reference) is not str:
            raise ProbeRefused("compose_inventory_unproved")
        image_id = value.get("Image")
        if type(image_id) is not str or _IMAGE_ID.fullmatch(image_id) is None:
            raise ProbeRefused("compose_inventory_unproved")
        direct_repository_image = image_reference == _REPOSITORY or (
            image_reference.startswith(f"{_REPOSITORY}:")
            or image_reference.startswith(f"{_REPOSITORY}@")
        )
        if (
            direct_repository_image
            and re.fullmatch(
                re.escape(_REPOSITORY) + r"@sha256:[0-9a-f]{64}", image_reference
            )
            is None
        ):
            raise ProbeRefused("compose_inventory_changed")
        if image_id not in recognized_image_ids and not direct_repository_image:
            if image_id not in repo_digests_by_image:
                digests = _json(
                    [
                        "docker",
                        "image",
                        "inspect",
                        "--format",
                        "{{json .RepoDigests}}",
                        image_id,
                    ]
                )
                if type(digests) is not list or any(
                    type(item) is not str for item in digests
                ):
                    raise ProbeRefused("compose_inventory_unproved")
                repo_digests_by_image[image_id] = tuple(digests)
            direct_repository_image = any(
                re.fullmatch(re.escape(_REPOSITORY) + r"@sha256:[0-9a-f]{64}", digest)
                is not None
                for digest in repo_digests_by_image[image_id]
            )
        if (
            identifier in expected_ids
            or image_id in recognized_image_ids
            or direct_repository_image
        ):
            repository_images[identifier] = (image_reference, image_id)
    if set(repository_images) != expected_ids:
        raise ProbeRefused("compose_inventory_changed")
    expected_image_ids = {binding.app_container_id: binding.app_image_id}
    expected_image_ids.update(
        {writer.container_id: writer.image_id for writer in binding.writers}
    )
    for identifier, image_id in expected_image_ids.items():
        if repository_images[identifier][1] != image_id:
            raise ProbeRefused("compose_inventory_changed")


def _inspect_probe(
    name: str,
    binding: CapturedNetworkBinding,
    image_id: str,
    hosts: dict[str, str],
    expected_args: tuple[str, ...],
    expected_environment: dict[str, str],
    expected_status: str,
) -> dict[str, object]:
    value = _json(["docker", "inspect", "--format", "{{json .}}", name])
    try:
        config, host = value["Config"], value["HostConfig"]
        network_settings = value["NetworkSettings"]
        probe_networks = (
            network_settings.get("Networks") if type(network_settings) is dict else None
        )
        if expected_status == "created":
            if probe_networks is None or probe_networks == {}:
                network_proven = True
            elif (
                type(probe_networks) is dict
                and set(probe_networks) == {binding.network_name}
                and type(probe_networks[binding.network_name]) is dict
                and probe_networks[binding.network_name].get("NetworkID")
                in ("", binding.network_id)
            ):
                network_proven = True
            else:
                network_proven = False
        else:
            network_proven = (
                type(probe_networks) is dict
                and set(probe_networks) == {binding.network_name}
                and type(probe_networks[binding.network_name]) is dict
                and probe_networks[binding.network_name].get("NetworkID")
                == binding.network_id
            )
        if (
            type(value) is not dict
            or type(config) is not dict
            or type(host) is not dict
            or type(network_settings) is not dict
            or not network_proven
            or type(value.get("State")) is not dict
            or value["State"].get("Status") != expected_status
            or type(value.get("Mounts")) is not list
            or value["Mounts"]
            or value.get("Name") != f"/{name}"
            or value.get("Image") != image_id
            or host.get("ReadonlyRootfs") is not True
            or host.get("NetworkMode") != binding.network_id
            or host.get("Privileged") is not False
            or host.get("PidMode") not in (None, "")
            or host.get("IpcMode") not in (None, "", "private")
            or host.get("UTSMode") not in (None, "")
            or host.get("Binds") not in (None, [])
            or host.get("VolumesFrom") not in (None, [])
            or host.get("Mounts") not in (None, [])
            or host.get("ExtraHosts")
            != [f"{host}:{address}" for host, address in sorted(hosts.items())]
            or host.get("CapAdd") not in (None, [])
            or type(host.get("CapDrop")) is not list
            or "ALL" not in host["CapDrop"]
            or "no-new-privileges:true" not in host.get("SecurityOpt", [])
            or config.get("WorkingDir") != "/app"
            or config.get("Entrypoint") != ["/opt/venv/bin/python"]
            or config.get("Cmd") != ["-I", "-B", "-c", _PROBE_CODE, *expected_args]
        ):
            raise ValueError
        env = config.get("Env")
        if type(env) is not list or any(type(item) is not str for item in env):
            raise ValueError
        env_map: dict[str, str] = {}
        for entry in env:
            key, separator, item = entry.partition("=")
            if not separator or key in env_map:
                raise ValueError
            env_map[key] = item
        if any(env_map.get(key) for key in _OVERLAY_KEYS):
            raise ValueError
        if any(env_map.get(key) != item for key, item in expected_environment.items()):
            raise ValueError
        if any(
            item.startswith("LD_PRELOAD=")
            and item.partition("=")[2]
            or item.startswith("LD_LIBRARY_PATH=")
            and item.partition("=")[2]
            for item in env
        ):
            raise ValueError
        return value
    except (KeyError, TypeError, ValueError):
        raise ProbeRefused("probe_configuration_unproved") from None


def _probe(
    binding: CapturedNetworkBinding,
    *,
    image: str,
    module: str,
    arguments: tuple[str, ...],
    operation: str,
) -> str:
    image_id = {
        "incumbent": binding.app_image_id,
        "candidate": binding.candidate_image_id,
    }.get(image)
    if image_id is None:
        raise ProbeRefused("probe_image_unproved")
    database_url, broker_url, broker_key = _verify(binding, allow_stopped_app=True)
    hosts = {
        binding.database_hostname: binding.database.ipv4_address,
        binding.broker_hostname: binding.broker.ipv4_address,
    }
    name = f"dotmac-sub-cutover-probe-{uuid.uuid4().hex}"
    command = [
        "docker",
        "create",
        "--name",
        name,
        "--read-only",
        "--network",
        binding.network_id,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--entrypoint",
        "/opt/venv/bin/python",
        "--workdir",
        "/app",
        "--env-file",
        "/dev/stdin",
    ]
    for hostname, address in sorted(hosts.items()):
        command.extend(("--add-host", f"{hostname}:{address}"))
    expected_args = (module, *arguments)
    command.extend(
        (
            image_id,
            "-I",
            "-B",
            "-c",
            _PROBE_CODE,
            *expected_args,
        )
    )
    expected_environment = {"DATABASE_URL": database_url, broker_key: broker_url}
    if module == "alembic" and arguments[:1] == ("heads",):
        expected_environment.update(_migration_graph_environment())
    environment_file = "".join(
        f"{key}={value}\n" for key, value in expected_environment.items()
    )
    try:
        created = _run(command, input_text=environment_file, side_effecting=True)
        if created.returncode:
            raise ProbeRefused("probe_create_refused")
        _inspect_probe(
            name,
            binding,
            image_id,
            hosts,
            expected_args,
            expected_environment,
            "created",
        )
        result = _run(
            ["docker", "start", "--attach", name],
            timeout=660,
            side_effecting=True,
        )
        if result.returncode:
            raise ProbeRefused("probe_operation_refused")
        value = _inspect_probe(
            name,
            binding,
            image_id,
            hosts,
            expected_args,
            expected_environment,
            "exited",
        )
        state = value["State"]
        if (
            type(state) is not dict
            or state.get("Running") is not False
            or state.get("Status") != "exited"
            or state.get("ExitCode") != 0
            or state.get("OOMKilled") is not False
        ):
            raise ProbeRefused("probe_operation_refused")
    finally:
        try:
            cleanup = _run(["docker", "rm", "--force", name], side_effecting=True)
        except ProbeRefused:
            raise ProbeIndeterminate("probe_cleanup_indeterminate") from None
        if cleanup.returncode:
            raise ProbeIndeterminate("probe_cleanup_indeterminate")
    _verify(binding, allow_stopped_app=True)
    return result.stdout


def _verify_cli(args: argparse.Namespace) -> int:
    binding = _binding_from_json(args.binding_json)
    _verify(binding, allow_stopped_app=True)
    print(json.dumps({"schema_version": 1, "outcome": "identity-proven"}))
    return 0


def _capture_cli(args: argparse.Namespace) -> int:
    binding = _capture(
        app_container=args.app_container,
        writer_specs=tuple(args.writer),
        candidate_reference=args.candidate_image,
        candidate_revision=args.candidate_revision,
        inactive_services=tuple(args.inactive_service),
        compose_arguments=tuple(args.compose_argument),
    )
    print(json.dumps(binding.public_json(), separators=(",", ":"), allow_nan=False))
    return 0


def _run_cli(args: argparse.Namespace) -> int:
    binding = _binding_from_json(args.binding_json)
    if args.operation == "verify":
        return _verify_cli(args)
    if args.operation == "drain":
        if args.mode not in {"workers-and-broker", "broker-only"}:
            raise ProbeRefused("invalid_drain_request")
        if args.drain_operation not in {"inspect", "wait-for-stable-empty"}:
            raise ProbeRefused("invalid_drain_request")
        if args.mode == "workers-and-broker" and not args.expected_node:
            raise ProbeRefused("invalid_drain_request")
        if args.mode == "broker-only" and args.expected_node:
            raise ProbeRefused("invalid_drain_request")
        drain_args = (
            args.drain_operation,
            "--mode",
            args.mode,
            *(
                part
                for node in args.expected_node
                for part in ("--expected-node", node)
            ),
            "--inspection-timeout",
            "5",
            "--drain-budget",
            str(args.drain_budget),
            "--stability-interval",
            "5",
        )
        output = _probe(
            binding,
            image=args.image,
            module="scripts.observe_writer_drain",
            arguments=drain_args,
            operation="drain",
        )
        try:
            evidence = json.loads(output)
            if type(evidence) is not dict or evidence.get("schema_version") != 1:
                raise ValueError
        except (TypeError, ValueError):
            raise ProbeRefused("drain_observation_unproved") from None
        print(json.dumps(evidence, separators=(",", ":"), allow_nan=False))
        return 0
    if args.operation not in _FLOOR_OPERATIONS:
        raise ProbeRefused("unsupported_probe_operation")
    module, arguments = _FLOOR_OPERATIONS[args.operation]
    output = _probe(
        binding,
        image=args.image,
        module=module,
        arguments=arguments,
        operation=args.operation,
    )
    if args.operation in {"payments-supported", "payments-installed"}:
        evidence = _validate_payments_floor_output(args.operation, output)
        print(json.dumps(evidence, separators=(",", ":"), allow_nan=False))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--app-container", required=True)
    capture.add_argument("--writer", action="append", default=[])
    capture.add_argument("--candidate-image", required=True)
    capture.add_argument("--candidate-revision", required=True)
    capture.add_argument("--inactive-service", action="append", default=[])
    capture.add_argument("--compose-argument", action="append", default=[])
    capture.set_defaults(handler=_capture_cli)
    run = commands.add_parser("run")
    run.add_argument("--binding-json", required=True)
    run.add_argument("--image", choices=("incumbent", "candidate"), required=True)
    run.add_argument(
        "--operation",
        choices=(*_FLOOR_OPERATIONS, "drain", "verify"),
        required=True,
    )
    run.add_argument("--mode", choices=("workers-and-broker", "broker-only"))
    run.add_argument(
        "--drain-operation",
        choices=("inspect", "wait-for-stable-empty"),
        default="inspect",
    )
    run.add_argument("--expected-node", action="append", default=[])
    run.add_argument("--drain-budget", type=int, default=600)
    run.set_defaults(handler=_run_cli)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        return args.handler(args)
    except ProbeIndeterminate:
        print("PRODUCTION CUTOVER PROBE INDETERMINATE", file=sys.stderr)
        return 3
    except (ProbeRefused, ValueError):
        print("PRODUCTION CUTOVER PROBE REFUSED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
