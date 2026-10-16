"""Hosted contract checks for the production cutover network probe."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import production_deploy_probe as probe


def _binding() -> probe.CapturedNetworkBinding:
    endpoint = probe.EndpointBinding(
        "a" * 64,
        "sha256:" + "b" * 64,
        "2026-10-15T00:00:00Z",
        0,
        "c" * 64,
        "172.20.0.2",
        ("postgres-local", "database"),
    )
    broker = probe.EndpointBinding(
        "d" * 64,
        "sha256:" + "e" * 64,
        "2026-10-15T00:00:00Z",
        0,
        "f" * 64,
        "172.20.0.3",
        ("redis-local", "broker"),
    )
    return probe.CapturedNetworkBinding(
        "1" * 64,
        "dotmac_default",
        "2" * 64,
        "sha256:" + "3" * 64,
        "sha256:" + "4" * 64,
        "5" * 40,
        "2026-10-15T00:00:00Z",
        0,
        "sha256:" + "6" * 64,
        "sha256:" + "7" * 64,
        "8" * 40,
        "postgres-local",
        endpoint,
        "redis-local",
        broker,
        (),
        (),
        ("--project-directory", "/srv/dotmac_sub", "-f", "compose.yml"),
        "postgresql://user:db-secret@postgres-local:5432/app",
        "redis://:redis-secret@redis-local:6379/10",
        "CELERY_BROKER_URL",
    )


def test_binding_artifact_and_repr_do_not_expose_database_or_broker_urls() -> None:
    binding = _binding()
    serialized = json.dumps(binding.public_json())
    representation = repr(binding)

    for secret in ("db-secret", "redis-secret", "postgresql://", "redis://"):
        assert secret not in serialized
        assert secret not in representation


def test_host_alias_that_resolves_to_two_containers_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    network_id = "1" * 64
    container_ids = ("a" * 64, "b" * 64)
    membership = {
        container_id: {
            "EndpointID": f"{index + 1:x}" * 64,
            "IPv4Address": f"172.20.0.{index + 2}/16",
        }
        for index, container_id in enumerate(container_ids)
    }

    def inspect(container_id: str) -> dict[str, object]:
        index = container_ids.index(container_id)
        endpoint_id = f"{index + 1:x}" * 64
        return {
            "Id": container_id,
            "Image": "sha256:" + f"{index + 3:x}" * 64,
            "RestartCount": 0,
            "State": {"Running": True, "StartedAt": "2026-10-15T00:00:00Z"},
            "Config": {"Env": [], "Labels": {}},
            "NetworkSettings": {
                "Networks": {
                    "dotmac_default": {
                        "NetworkID": network_id,
                        "EndpointID": endpoint_id,
                        "IPAddress": f"172.20.0.{index + 2}",
                        "Aliases": ["database"],
                    }
                }
            },
        }

    monkeypatch.setattr(
        probe, "_network", lambda _network_id: ("dotmac_default", membership)
    )
    monkeypatch.setattr(probe, "_inspect", inspect)

    with pytest.raises(probe.ProbeRefused, match="runtime_endpoint_unproved"):
        probe._endpoint(
            "database", network_id=network_id, network_name="dotmac_default"
        )


def test_readonly_probe_refuses_an_image_volume_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    expected_hosts = {
        binding.database_hostname: binding.database.ipv4_address,
        binding.broker_hostname: binding.broker.ipv4_address,
    }
    monkeypatch.setattr(
        probe,
        "_json",
        lambda _command: {
            "Config": {},
            "HostConfig": {},
            "NetworkSettings": {"Networks": {}},
            "State": {},
            "Mounts": [{"Type": "volume", "Destination": "/app/data"}],
            "Name": "/probe",
            "Image": binding.candidate_image_id,
        },
    )

    with pytest.raises(probe.ProbeRefused, match="probe_configuration_unproved"):
        probe._inspect_probe(
            "probe",
            binding,
            binding.candidate_image_id,
            expected_hosts,
            ("scripts.verify_payments_intent_rollback",),
            {},
            "created",
        )


def _probe_inspect_value(
    binding: probe.CapturedNetworkBinding,
    *,
    network_name: str | None,
    network_id: str,
) -> dict[str, object]:
    hosts = {
        binding.database_hostname: binding.database.ipv4_address,
        binding.broker_hostname: binding.broker.ipv4_address,
    }
    return {
        "Config": {
            "WorkingDir": "/app",
            "Entrypoint": ["/opt/venv/bin/python"],
            "Cmd": [
                "-I",
                "-B",
                "-c",
                probe._PROBE_CODE,
                "scripts.verify_payments_intent_rollback",
            ],
            "Env": [
                f"DATABASE_URL={binding.database_url}",
                f"{binding.broker_env_key}={binding.broker_url}",
            ],
        },
        "HostConfig": {
            "ReadonlyRootfs": True,
            "NetworkMode": binding.network_id,
            "Privileged": False,
            "ExtraHosts": [
                f"{host}:{address}" for host, address in sorted(hosts.items())
            ],
            "CapAdd": [],
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Binds": [],
            "VolumesFrom": [],
        },
        "NetworkSettings": {
            "Networks": (
                {}
                if network_name is None
                else {network_name: {"NetworkID": network_id}}
            ),
        },
        "State": {"Status": "created"},
        "Mounts": [],
        "Name": "/probe",
        "Image": binding.candidate_image_id,
    }


@pytest.mark.parametrize("network_name", [None, "dotmac_default"])
def test_created_probe_accepts_docker_empty_endpoint_network(
    network_name: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    value = _probe_inspect_value(binding, network_name=network_name, network_id="")
    monkeypatch.setattr(probe, "_json", lambda _command: value)

    assert (
        probe._inspect_probe(
            "probe",
            binding,
            binding.candidate_image_id,
            {
                binding.database_hostname: binding.database.ipv4_address,
                binding.broker_hostname: binding.broker.ipv4_address,
            },
            ("scripts.verify_payments_intent_rollback",),
            {
                "DATABASE_URL": binding.database_url,
                binding.broker_env_key: binding.broker_url,
            },
            "created",
        )
        == value
    )


@pytest.mark.parametrize(
    ("network_name", "network_id"),
    [("other-network", ""), ("dotmac_default", "f" * 64)],
)
def test_created_probe_rejects_wrong_network_attachment(
    network_name: str, network_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _binding()
    value = _probe_inspect_value(
        binding, network_name=network_name, network_id=network_id
    )
    monkeypatch.setattr(probe, "_json", lambda _command: value)

    with pytest.raises(probe.ProbeRefused, match="probe_configuration_unproved"):
        probe._inspect_probe(
            "probe",
            binding,
            binding.candidate_image_id,
            {
                binding.database_hostname: binding.database.ipv4_address,
                binding.broker_hostname: binding.broker.ipv4_address,
            },
            ("scripts.verify_payments_intent_rollback",),
            {
                "DATABASE_URL": binding.database_url,
                binding.broker_env_key: binding.broker_url,
            },
            "created",
        )


def test_fixed_inventory_refuses_previously_absent_inactive_service_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    image = f"{probe._REPOSITORY}@{binding.candidate_digest}"
    monkeypatch.setattr(
        probe,
        "_compose_services",
        lambda *_args, **_kwargs: {
            "app": {"image": image},
            "team-inbox-smtp": {"image": image},
        },
    )
    commands: list[list[str]] = []

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        output = (
            binding.app_container_id + "\n" if command[-1] == "app" else "9" * 64 + "\n"
        )
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(probe, "_run", run)

    with pytest.raises(probe.ProbeRefused, match="compose_inventory_changed"):
        probe.verify_fixed_compose_inventory(
            replace(
                binding,
                inactive_services=("team-inbox-smtp",),
            )
        )

    assert len(commands) == 2
    assert commands[-1][-1] == "team-inbox-smtp"


def test_fixed_inventory_refuses_new_app_image_service_in_effective_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    image = f"{probe._REPOSITORY}@{binding.candidate_digest}"
    monkeypatch.setattr(
        probe,
        "_compose_services",
        lambda *_args, **_kwargs: {
            "app": {"image": image},
            "new-producer": {"image": f"{probe._REPOSITORY}:legacy"},
        },
    )
    monkeypatch.setattr(
        probe,
        "_run",
        lambda command: subprocess.CompletedProcess(command, 0, "", ""),
    )

    with pytest.raises(probe.ProbeRefused, match="compose_inventory_unproved"):
        probe.verify_fixed_compose_inventory(binding)


def test_fixed_inventory_requests_full_container_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    image = f"{probe._REPOSITORY}@{binding.candidate_digest}"
    monkeypatch.setattr(
        probe,
        "_compose_services",
        lambda *_args, **_kwargs: {"app": {"image": image}},
    )
    commands: list[list[str]] = []

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            binding.app_container_id + "\n",
            "",
        )

    monkeypatch.setattr(probe, "_run", run)
    monkeypatch.setattr(
        probe,
        "_json",
        lambda _command: {
            "Id": binding.app_container_id,
            "Image": binding.app_image_id,
            "Config": {"Image": f"{probe._REPOSITORY}@{binding.app_digest}"},
        },
    )

    probe.verify_fixed_compose_inventory(binding)

    global_inventory = commands[-1]
    assert global_inventory[:3] == ["docker", "ps", "-a"]
    assert "--no-trunc" in global_inventory
    assert "--filter" not in global_inventory


def test_fixed_inventory_catches_extra_container_started_by_app_image_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    image = f"{probe._REPOSITORY}@{binding.candidate_digest}"
    rogue_id = "9" * 64
    monkeypatch.setattr(
        probe,
        "_compose_services",
        lambda *_args, **_kwargs: {"app": {"image": image}},
    )

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        output = (
            binding.app_container_id + "\n"
            if command[1] == "compose"
            else f"{binding.app_container_id}\n{rogue_id}\n"
        )
        return subprocess.CompletedProcess(command, 0, output, "")

    def inspect(command: list[str]) -> dict[str, object]:
        identifier = command[-1]
        is_app = identifier == binding.app_container_id
        return {
            "Id": identifier,
            "Image": binding.app_image_id if is_app else binding.candidate_image_id,
            "Config": {
                "Image": image if is_app else binding.candidate_image_id,
            },
        }

    monkeypatch.setattr(probe, "_run", run)
    monkeypatch.setattr(probe, "_json", inspect)

    with pytest.raises(probe.ProbeRefused, match="compose_inventory_changed"):
        probe.verify_fixed_compose_inventory(binding)


def test_probe_after_start_requires_populated_captured_network_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    value = _probe_inspect_value(
        binding, network_name=binding.network_name, network_id=""
    )
    value["State"] = {"Status": "exited"}
    monkeypatch.setattr(probe, "_json", lambda _command: value)

    with pytest.raises(probe.ProbeRefused, match="probe_configuration_unproved"):
        probe._inspect_probe(
            "probe",
            binding,
            binding.candidate_image_id,
            {
                binding.database_hostname: binding.database.ipv4_address,
                binding.broker_hostname: binding.broker.ipv4_address,
            },
            ("scripts.verify_payments_intent_rollback",),
            {
                "DATABASE_URL": binding.database_url,
                binding.broker_env_key: binding.broker_url,
            },
            "exited",
        )


def test_timed_out_docker_mutation_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired("docker create", 30)

    monkeypatch.setattr(probe.subprocess, "run", timeout)

    with pytest.raises(probe.ProbeIndeterminate):
        probe._run(["docker", "create"], side_effecting=True)


def test_candidate_compose_environment_rejects_unresolved_values() -> None:
    with pytest.raises(probe.ProbeRefused, match="compose_configuration_unproved"):
        probe._compose_environment({"environment": {"DATABASE_URL": None}})


def test_binding_cannot_inject_arbitrary_compose_flags_or_source_files() -> None:
    with pytest.raises(probe.ProbeRefused, match="compose_configuration_unproved"):
        probe._validate_compose_arguments(
            (
                "--project-directory",
                "/srv/dotmac_sub",
                "--env-file",
                "/var/attacker.env",
                "-f",
                "/var/docker-compose.yml",
                "--profile",
                "attacker",
            )
        )


def test_payments_supported_operation_requires_complete_predecessor_or_installed_floor() -> (
    None
):
    predecessor = {
        "schema_version": 1,
        "floor": "predecessor",
        "observation": {
            "legacy_shape_complete": True,
            "installed_shape_complete": False,
            "correlation_columns": 0,
            "correlation_constraints": 0,
            "correlation_triggers": 0,
            "correlation_functions": 0,
        },
    }
    installed = {
        "schema_version": 1,
        "floor": "installed",
        "observation": {
            "legacy_shape_complete": True,
            "installed_shape_complete": True,
            "correlation_columns": 2,
            "correlation_constraints": 2,
            "correlation_triggers": 1,
            "correlation_functions": 1,
        },
    }
    assert (
        probe._validate_payments_floor_output(
            "payments-supported", json.dumps(predecessor)
        )
        == predecessor
    )
    assert (
        probe._validate_payments_floor_output(
            "payments-supported", json.dumps(installed)
        )
        == installed
    )
    assert (
        probe._validate_payments_floor_output(
            "payments-installed", json.dumps(installed)
        )
        == installed
    )
    for invalid in (
        {
            **installed,
            "observation": {**installed["observation"], "legacy_shape_complete": False},
        },
        {
            **installed,
            "observation": {**installed["observation"], "correlation_functions": 0},
        },
        {**predecessor, "floor": "unknown"},
        {
            **predecessor,
            "observation": {**predecessor["observation"], "correlation_columns": 1},
        },
        {
            **predecessor,
            "observation": {**predecessor["observation"], "correlation_columns": False},
        },
    ):
        with pytest.raises(probe.ProbeRefused, match="payments_floor_unproved"):
            probe._validate_payments_floor_output(
                "payments-supported", json.dumps(invalid)
            )


def test_migration_graph_environment_is_exactly_the_canonical_pointer_set() -> None:
    values = probe._migration_graph_environment()
    assert set(values) == {
        "DOTMAC_MIGRATION_BINDINGS",
        "DOTMAC_MODULE_PLANE_SELECTIONS",
    }
    assert values["DOTMAC_MIGRATION_BINDINGS"] == (
        "app.migration_bindings:ASSEMBLY_PREREQUISITE_BINDINGS"
    )
    assert values["DOTMAC_MODULE_PLANE_SELECTIONS"] == (
        "app.migration_bindings:ASSEMBLY_MODULE_PLANES"
    )


def test_candidate_heads_probe_forwards_canonical_graph_pointers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    monkeypatch.setattr(
        probe,
        "_verify",
        lambda *_args, **_kwargs: (
            binding.database_url,
            binding.broker_url,
            binding.broker_env_key,
        ),
    )
    commands: list[tuple[list[str], str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append((command, str(kwargs.get("input_text", ""))))
        return subprocess.CompletedProcess(command, 0, "heads\n", "")

    inspections: list[dict[str, str]] = []

    def inspect(
        _name: str,
        _binding: probe.CapturedNetworkBinding,
        _image_id: str,
        _hosts: dict[str, str],
        _args: tuple[str, ...],
        environment: dict[str, str],
        status: str,
    ) -> dict[str, object]:
        inspections.append(environment)
        if status == "exited":
            return {
                "State": {
                    "Running": False,
                    "Status": "exited",
                    "ExitCode": 0,
                    "OOMKilled": False,
                }
            }
        return {}

    monkeypatch.setattr(probe, "_run", run)
    monkeypatch.setattr(probe, "_inspect_probe", inspect)

    assert (
        probe._probe(
            binding,
            image="candidate",
            module="alembic",
            arguments=("heads", "--resolve-dependencies"),
            operation="migration-head-observation",
        )
        == "heads\n"
    )
    expected = probe._migration_graph_environment()
    assert len(inspections) == 2
    assert all(
        all(environment.get(key) == value for key, value in expected.items())
        for environment in inspections
    )
    create_input = next(value for command, value in commands if command[1] == "create")
    assert all(f"{key}={value}\n" in create_input for key, value in expected.items())


def test_database_url_host_override_and_custom_runtime_dns_are_refused() -> None:
    with pytest.raises(probe.ProbeRefused, match="runtime_endpoint_unproved"):
        probe._url_host(
            "postgresql://user:password@postgres-local/app?hostaddr=172.20.0.99",
            database=True,
        )
    with pytest.raises(probe.ProbeRefused, match="unsupported_name_resolution"):
        probe._require_default_name_resolution({"HostConfig": {"Dns": ["192.0.2.53"]}})


def test_production_deadline_uses_exact_server_recorded_job_start() -> None:
    workflow = (
        Path(probe.__file__).resolve().parents[1]
        / ".github/workflows/production-deploy.yml"
    ).read_text()

    assert "listJobsForWorkflowRunAttempt" in workflow
    assert "run_attempt: context.runAttempt" in workflow
    assert 'job.name === "deploy"' in workflow
    assert 'matches[0].status !== "in_progress"' in workflow
    assert "matches[0].started_at" in workflow
    assert "String(Math.floor(started / 1000) + 120 * 60)" in workflow
    assert (
        "PRODUCTION_DEPLOY_DEADLINE_EPOCH: ${{ steps.job-budget.outputs.deadline_epoch }}"
        in workflow
    )
    assert 'started="$(date -u +%s)"' not in workflow


def _runtime_with_host_maps(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    probe.CapturedNetworkBinding, dict[str, dict[str, object]], dict[str, object]
]:
    host_map = probe._extra_hosts(["Telemetry.Example.:192.0.2.50"])
    writer = probe.WriterBinding(
        "worker",
        "9" * 64,
        "sha256:" + "a" * 64,
        "2026-10-15T00:00:00Z",
        0,
        "b" * 64,
        "172.20.0.5",
        ("worker",),
        host_map,
    )
    binding = replace(_binding(), writers=(writer,), app_extra_hosts=host_map)
    environment = {
        "DATABASE_URL": binding.database_url,
        "CELERY_BROKER_URL": binding.broker_url,
    }
    containers: dict[str, dict[str, object]] = {}
    members: dict[str, object] = {}

    def container(
        identifier: str,
        image: str,
        name: str,
        started: str,
        endpoint_id: str,
        address: str,
        aliases: tuple[str, ...],
        *,
        hosts: bool,
    ) -> None:
        containers[identifier] = {
            "Id": identifier,
            "Image": image,
            "Name": "/" + name,
            "RestartCount": 0,
            "State": {"Running": True, "Status": "running", "StartedAt": started},
            "Config": {
                "Env": [f"{key}={value}" for key, value in environment.items()],
                "Image": f"{probe._REPOSITORY}@{binding.app_digest}",
                "Labels": {probe._REVISION_LABEL: binding.app_revision},
            },
            "HostConfig": {
                "ExtraHosts": ["TELEMETRY.example.:192.0.2.50"] if hosts else []
            },
            "NetworkSettings": {
                "Networks": {
                    binding.network_name: {
                        "NetworkID": binding.network_id,
                        "EndpointID": endpoint_id,
                        "IPAddress": address,
                        "Aliases": list(aliases),
                        "DNSNames": [*aliases, name + "-dns"],
                    }
                }
            },
        }
        members[identifier] = {
            "Name": name,
            "EndpointID": endpoint_id,
            "IPv4Address": address + "/16",
        }

    container(
        binding.app_container_id,
        binding.app_image_id,
        probe._APP_NAME,
        binding.app_started_at,
        "e" * 64,
        "172.20.0.4",
        ("app",),
        hosts=True,
    )
    container(
        writer.container_id,
        writer.image_id,
        "writer-container",
        writer.started_at,
        writer.endpoint_id,
        writer.ipv4_address,
        writer.aliases,
        hosts=True,
    )
    for endpoint, name in (
        (binding.database, "database-container"),
        (binding.broker, "broker-container"),
    ):
        container(
            endpoint.container_id,
            endpoint.image_id,
            name,
            endpoint.started_at,
            endpoint.endpoint_id,
            endpoint.ipv4_address,
            endpoint.aliases,
            hosts=False,
        )
    services: dict[str, object] = {
        service: {
            "environment": dict(environment),
            "extra_hosts": {"telemetry.example": "192.0.2.50"},
        }
        for service in ("app", "worker")
    }
    monkeypatch.setattr(probe, "_inspect", lambda identifier: containers[identifier])
    monkeypatch.setattr(
        probe, "_inspect_named", lambda name: containers[binding.app_container_id]
    )
    monkeypatch.setattr(
        probe, "_network", lambda identifier: (binding.network_name, members)
    )
    monkeypatch.setattr(
        probe,
        "_candidate",
        lambda *args: (binding.candidate_image_id, binding.candidate_digest, {}),
    )
    monkeypatch.setattr(probe, "_compose_services", lambda *args, **kwargs: services)
    return binding, containers, services


def test_noncritical_extra_hosts_are_captured_and_reverified_from_trusted_compose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding, _containers, _services = _runtime_with_host_maps(monkeypatch)
    captured = probe._capture(
        app_container=binding.app_container_id,
        writer_specs=("worker=" + binding.writers[0].container_id,),
        candidate_reference=f"{probe._REPOSITORY}@{binding.candidate_digest}",
        candidate_revision=binding.candidate_revision,
        inactive_services=(),
        compose_arguments=binding.compose_arguments,
    )
    assert captured.app_extra_hosts == binding.app_extra_hosts
    assert captured.writers[0].extra_hosts == binding.writers[0].extra_hosts
    assert probe._verify(captured, allow_stopped_app=False) == (
        binding.database_url,
        binding.broker_url,
        binding.broker_env_key,
    )
    restored = probe._binding_from_json(json.dumps(captured.public_json()))
    assert restored.app_extra_hosts == captured.app_extra_hosts
    assert restored.writers[0].extra_hosts == captured.writers[0].extra_hosts


@pytest.mark.parametrize("service", ["app", "worker"])
@pytest.mark.parametrize("changed", ["actual", "captured", "target"])
def test_extra_host_map_drift_is_refused_for_app_and_each_writer(
    monkeypatch: pytest.MonkeyPatch,
    service: str,
    changed: str,
) -> None:
    binding, containers, services = _runtime_with_host_maps(monkeypatch)
    other = probe._extra_hosts({"telemetry.example": "192.0.2.51"})
    identifier = (
        binding.app_container_id
        if service == "app"
        else binding.writers[0].container_id
    )
    if changed == "actual":
        containers[identifier]["HostConfig"] = {
            "ExtraHosts": ["telemetry.example:192.0.2.51"]
        }
    elif changed == "target":
        services[service]["extra_hosts"] = {"telemetry.example": "192.0.2.51"}
    elif service == "app":
        binding = replace(binding, app_extra_hosts=other)
    else:
        binding = replace(
            binding, writers=(replace(binding.writers[0], extra_hosts=other),)
        )
    with pytest.raises(probe.ProbeRefused, match="extra_hosts_binding_changed"):
        probe._verify(binding, allow_stopped_app=False)


@pytest.mark.parametrize(
    "host",
    [
        "POSTGRES-LOCAL.",
        "DATABASE.",
        "database-container",
        "DATABASE-CONTAINER-DNS.",
        "broker",
    ],
)
@pytest.mark.parametrize("service", ["app", "worker"])
def test_critical_url_alias_container_and_dns_names_cannot_be_overridden(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    service: str,
) -> None:
    binding, containers, services = _runtime_with_host_maps(monkeypatch)
    mapping = probe._extra_hosts({host: "192.0.2.90"})
    identifier = (
        binding.app_container_id
        if service == "app"
        else binding.writers[0].container_id
    )
    containers[identifier]["HostConfig"] = {"ExtraHosts": [host + ":192.0.2.90"]}
    services[service]["extra_hosts"] = {host: "192.0.2.90"}
    if service == "app":
        binding = replace(binding, app_extra_hosts=mapping)
    else:
        binding = replace(
            binding, writers=(replace(binding.writers[0], extra_hosts=mapping),)
        )
    with pytest.raises(probe.ProbeRefused, match="critical_name_override"):
        probe._verify(binding, allow_stopped_app=False)


@pytest.mark.parametrize(
    "value",
    [
        ["host:host-gateway"],
        ["host:192.0.2.1", "HOST.:192.0.2.1"],
        ["host:192.0.2.1", "HOST.:192.0.2.2"],
        ["bad host:192.0.2.1"],
        ["host:not-an-ip"],
        ["host:fe80::1%eth0"],
        ["host..:192.0.2.1"],
        ["höst:192.0.2.1"],
        ["host"],
        {"host": ["192.0.2.1"]},
    ],
)
def test_extra_hosts_rejects_gateways_duplicates_and_malformed_mappings(
    value: object,
) -> None:
    with pytest.raises(probe.ProbeRefused, match="unsupported_name_resolution"):
        probe._extra_hosts(value)


def test_extra_hosts_canonicalizes_ascii_hosts_and_literal_ipv6() -> None:
    assert probe._extra_hosts(["HOST.example.=[2001:0db8::1]"]) == (
        probe.ExtraHostBinding("host.example", "2001:db8::1"),
    )


@pytest.mark.parametrize("key", ["Dns", "DnsSearch", "DnsOptions", "Links"])
def test_noncritical_mapping_does_not_allow_custom_dns_or_links(key: str) -> None:
    with pytest.raises(probe.ProbeRefused, match="unsupported_name_resolution"):
        probe._require_default_name_resolution(
            {
                "HostConfig": {
                    "ExtraHosts": ["telemetry.example:192.0.2.50"],
                    key: ["custom"],
                }
            }
        )


def test_old_binding_omissions_mean_empty_only_and_duplicate_json_keys_refuse() -> None:
    value = _binding().public_json()
    value.pop("app_extra_hosts")
    old = probe._binding_from_json(json.dumps(value))
    assert old.app_extra_hosts == ()
    source = json.dumps(value)
    source = source.replace(
        '"schema_version": 1', '"schema_version": 1, "schema_version": 1'
    )
    with pytest.raises(probe.ProbeRefused, match="captured_binding_unproved"):
        probe._binding_from_json(source)


def test_legacy_empty_binding_cannot_adopt_existing_nonempty_runtime_maps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding, _containers, _services = _runtime_with_host_maps(monkeypatch)
    value = binding.public_json()
    value.pop("app_extra_hosts")
    for writer in value["writers"]:
        writer.pop("extra_hosts")
    old = probe._binding_from_json(json.dumps(value))
    assert old.app_extra_hosts == () and old.writers[0].extra_hosts == ()
    with pytest.raises(probe.ProbeRefused, match="extra_hosts_binding_changed"):
        probe._verify(old, allow_stopped_app=False)


def test_compose_render_remains_private_and_retains_only_canonical_host_projection(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(probe, "_validate_compose_arguments", lambda arguments: None)
    rendered = {
        "services": {
            "app": {
                "environment": {"PRIVATE_TEST_VALUE": "opaque-private-canary"},
                "extra_hosts": {"Telemetry.Example.": "192.0.2.50"},
            }
        }
    }
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            json.dumps(rendered),
            "private-stderr-canary",
        ),
    )
    services = probe._compose_services(
        (), image_reference="candidate", revision="a" * 40
    )
    assert probe._compose_extra_hosts(services["app"]) == (
        probe.ExtraHostBinding("telemetry.example", "192.0.2.50"),
    )
    assert capsys.readouterr() == ("", "")
