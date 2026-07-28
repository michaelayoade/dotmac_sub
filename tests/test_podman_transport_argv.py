"""The podman argv is hardened and carries no secret. No Podman needed here.

These pin the security flags in isolation so a regression that weakens the
container's confinement fails a fast unit test rather than only showing up as a
live containment hole.
"""

from __future__ import annotations

from app.services.integrations.podman_transport import (
    SECRET_ENV_PREFIX,
    PodmanTransport,
    _build_argv,
)

IMAGE = "ghcr.io/dotmac/connector-example@sha256:" + "a" * 64


def _argv(**overrides) -> list[str]:
    params = {"deadline_seconds": 30, "env_file": "/run/user/1000/secret.env"}
    params.update(overrides)
    return _build_argv(IMAGE, **params)


def test_argv_drops_capabilities_and_new_privileges():
    argv = _argv()
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv


def test_argv_uses_a_read_only_rootfs_with_only_an_in_memory_scratch():
    argv = _argv()
    assert "--read-only" in argv
    tmpfs = next(a for a in argv if a.startswith("--tmpfs="))
    assert "noexec" in tmpfs and "nosuid" in tmpfs


def test_argv_bounds_memory_and_pids_by_default():
    argv = _argv(memory="128m", pids_limit=64)
    assert "--memory=128m" in argv
    assert "--pids-limit=64" in argv


def test_cpu_limit_is_opt_in_because_it_needs_controller_delegation():
    # memory and pids are delegated to a rootless user out of the box; cpu is
    # not on a default Ubuntu host, so applying it unconditionally would make
    # every operation fail. Default omits it; an opted-in deployment sets it.
    assert not any(a.startswith("--cpus=") for a in _argv())
    assert "--cpus=0.5" in _argv(cpus="0.5")


def test_argv_sets_the_container_deadline():
    assert "--timeout=30" in _argv(deadline_seconds=30)


def test_argv_delivers_secrets_by_env_file_never_on_the_command_line():
    argv = _argv(env_file="/run/user/1000/s.env")
    assert "--env-file=/run/user/1000/s.env" in argv
    # No secret value or inline env assignment may appear as an argument.
    assert not any(a.startswith("--env=") or a == "-e" for a in argv)


def test_argv_pins_the_image_by_digest_as_the_final_argument():
    argv = _argv()
    assert argv[-1] == IMAGE
    assert "@sha256:" in argv[-1]


def test_argv_runs_interactive_and_removes_the_container():
    argv = _argv()
    assert "--rm" in argv
    assert "--interactive" in argv


def test_network_is_omitted_by_default_and_set_when_given():
    assert not any(a.startswith("--network=") for a in _argv())
    assert "--network=none" in _argv(network="none")


def test_a_sub_second_deadline_is_floored_to_one_second():
    assert "--timeout=1" in _argv(deadline_seconds=0)


def test_secret_env_prefix_is_stable():
    # The connector contract depends on this prefix; changing it is a breaking
    # change to every connector image.
    assert SECRET_ENV_PREFIX == "DM_SECRET_"


def test_a_custom_podman_path_replaces_the_binary():
    transport = PodmanTransport(podman_path="/usr/bin/podman")
    argv = list(transport._argv(IMAGE, 30, "/run/user/1000/s.env", "none"))
    assert argv[0] == "/usr/bin/podman"
