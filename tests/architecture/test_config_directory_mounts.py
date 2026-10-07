"""Release-tracked service config must be mounted so a pull is not invisible.

A single-file bind mount pins the inode that existed when the container was
created. ``git pull`` replaces a changed tracked file with a NEW inode, so the
running container keeps reading the old content; only restarting it picks the
change up. On dotmac_erp, vmagent kept a two-week-old config that way and
merged production and staging metrics into one series (dotmac_erp PR #695).

Two rules, both pinned here:

1. Checkout config (``./config``, ``./docker``) is bind-mounted as a
   directory. The only exceptions are FreeRADIUS's single-file overlays on the
   image's stock raddb tree, allow-listed below with the reason, and all
   read-only.
2. Mounting is not loading. None of freeradius, vmagent or promtail re-reads
   its config while running, and a deploy never recreates them, so
   ``scripts/deploy.sh`` restarts each one whose mounted config changed, and
   validates FreeRADIUS config before restarting it.

See docs/runbooks/SERVICE_CONFIG_MOUNTS.md.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = sorted(ROOT.glob("docker-compose*.yml"))
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy.sh"
CHECKOUT_CONFIG_ROOTS = ("./config/", "./docker/")

#: The single-file mounts that are deliberately NOT directories. FreeRADIUS
#: overlays individual files on the image's stock raddb tree (/etc/raddb is a
#: symlink to /etc/freeradius in freeradius/freeradius-server). Mounting a
#: directory over /etc/freeradius, mods-enabled or sites-enabled would hide the
#: stock modules, policies, certs and the inner-tunnel virtual server this
#: config depends on. The pinned inode is harmless here: FreeRADIUS reads these
#: files only at start, a HUP does not reload them, and the restart that loads
#: them re-resolves every source path. Every entry must stay read-only, and the
#: exact container paths are pinned so a typo cannot silently fall back to the
#: image's stock file.
SINGLE_FILE_MOUNT_EXCEPTIONS = {
    "freeradius": {
        ("./config/freeradius/radiusd.conf", "/etc/freeradius/radiusd.conf"),
        ("./config/freeradius/mods-enabled/sql", "/etc/raddb/mods-enabled/sql"),
        (
            "./config/freeradius/mods-enabled/sql_admin",
            "/etc/raddb/mods-enabled/sql_admin",
        ),
        (
            "./config/freeradius/sites-enabled/default",
            "/etc/raddb/sites-enabled/default",
        ),
        (
            "./config/freeradius/sites-enabled/admin-login",
            "/etc/raddb/sites-enabled/admin-login",
        ),
        ("./config/freeradius/dictionary.mikrotik", "/etc/raddb/dictionary.mikrotik"),
        ("./config/freeradius/dictionary", "/etc/raddb/dictionary"),
    },
}

#: service -> (checkout directory, container directory, config file name,
#: command flag naming the config file). Every file in a mounted directory is
#: readable by the container, so the directory contents are pinned too: a new
#: file there must be a deliberate, reviewed addition.
DIRECTORY_MOUNTED_AGENTS = {
    "vmagent": (
        "./config/vmagent",
        "/etc/vmagent",
        "config.yml",
        "-promscrape.config=",
    ),
    "promtail": (
        "./config/promtail",
        "/etc/promtail",
        "promtail-config.yml",
        "-config.file=",
    ),
}
EXPECTED_DIRECTORY_CONTENTS = {
    "vmagent": {"config.yml"},
    "promtail": {"promtail-config.yml"},
}
#: Services whose release-tracked config the deploy must apply on change.
CONFIG_RESTART_SERVICES = {"vmagent", "promtail", "freeradius"}


def _services(path: Path) -> dict:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(
        "services", {}
    ) or {}


def _bind_mounts(service: dict) -> list[tuple[str, str, bool]]:
    """Return (source, target, read_only) for every bind mount."""

    mounts = []
    for volume in service.get("volumes", []) or []:
        if isinstance(volume, str):
            parts = volume.split(":")
            if len(parts) >= 2 and parts[0].startswith((".", "/", "~")):
                options = parts[2].split(",") if len(parts) > 2 else []
                mounts.append((parts[0], parts[1], "ro" in options))
        elif isinstance(volume, dict) and volume.get("type") == "bind":
            mounts.append(
                (volume["source"], volume["target"], bool(volume.get("read_only")))
            )
    return mounts


def _command(service: dict) -> list[str]:
    command = service.get("command", [])
    return command.split() if isinstance(command, str) else list(command)


def _deploy_array(name: str) -> set[str]:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    declared = re.search(rf"^{name}=\(([^)]*)\)", script, re.M)
    assert declared, f"deploy.sh no longer declares {name}"
    return set(declared.group(1).split())


def test_compose_files_are_discovered() -> None:
    assert ROOT / "docker-compose.yml" in COMPOSE_FILES


@pytest.mark.parametrize("compose", COMPOSE_FILES, ids=lambda p: p.name)
def test_checkout_config_is_never_mounted_as_an_unlisted_single_file(
    compose: Path,
) -> None:
    offenders = []
    for name, service in _services(compose).items():
        allowed = SINGLE_FILE_MOUNT_EXCEPTIONS.get(name, set())
        for source, target, _read_only in _bind_mounts(service or {}):
            if not source.startswith(CHECKOUT_CONFIG_ROOTS):
                continue
            if (ROOT / source).is_dir() or (source, target) in allowed:
                continue
            offenders.append(f"{name}: {source}:{target}")
    assert offenders == [], (
        "a single-file bind mount goes stale when `git pull` replaces the "
        "file's inode; mount the containing directory, or add a reasoned "
        f"entry to SINGLE_FILE_MOUNT_EXCEPTIONS: {offenders}"
    )


def test_freeradius_single_file_overlays_are_exact_and_read_only() -> None:
    service = _services(ROOT / "docker-compose.yml")["freeradius"]
    checkout_mounts = [
        mount
        for mount in _bind_mounts(service)
        if mount[0].startswith(CHECKOUT_CONFIG_ROOTS)
    ]

    assert {(source, target) for source, target, _ in checkout_mounts} == (
        SINGLE_FILE_MOUNT_EXCEPTIONS["freeradius"]
    )
    writable = [
        source for source, _target, read_only in checkout_mounts if not read_only
    ]
    assert writable == [], (
        f"nothing writes FreeRADIUS config; mount it read-only: {writable}"
    )
    for source, _target, _read_only in checkout_mounts:
        assert (ROOT / source).is_file(), source


@pytest.mark.parametrize("agent", sorted(DIRECTORY_MOUNTED_AGENTS))
def test_agent_config_is_mounted_as_a_directory(agent: str) -> None:
    source, target, filename, flag = DIRECTORY_MOUNTED_AGENTS[agent]
    service = _services(ROOT / "docker-compose.yml")[agent]
    mounts = [(s, t) for s, t, _ in _bind_mounts(service)]
    read_only = {(s, t): ro for s, t, ro in _bind_mounts(service)}

    assert (source, target) in mounts, mounts
    assert read_only[(source, target)]
    assert (ROOT / source).is_dir()
    # No other mount reaches into the config directory as a single file.
    assert not [
        m
        for m in mounts
        if m[0].startswith(source + "/") or m[1].startswith(target + "/")
    ], mounts
    # The agent reads its config through the directory mount.
    assert f"{flag}{target}/{filename}" in _command(service)
    assert (ROOT / source / filename).is_file()


@pytest.mark.parametrize("agent", sorted(DIRECTORY_MOUNTED_AGENTS))
def test_mounted_config_directory_exposes_only_the_agent_config(agent: str) -> None:
    source = DIRECTORY_MOUNTED_AGENTS[agent][0]
    # .DS_Store is Finder noise on developer checkouts, never deployed content.
    contents = {p.name for p in (ROOT / source).iterdir()} - {".DS_Store"}
    assert contents == EXPECTED_DIRECTORY_CONTENTS[agent], (
        f"{source} is mounted into the {agent} container in full; anything "
        "added here (especially a secret) becomes readable by it"
    )


def test_deploy_applies_changed_config_for_every_config_service() -> None:
    """A mount makes the change visible; only a restart makes it loaded."""

    assert _deploy_array("CONFIG_RESTART_SERVICES") == CONFIG_RESTART_SERVICES
    compose = _services(ROOT / "docker-compose.yml")
    for service in CONFIG_RESTART_SERVICES:
        assert any(
            source.startswith(CHECKOUT_CONFIG_ROOTS)
            for source, _target, _ro in _bind_mounts(compose[service])
        ), service
    # Still not recreated by a deploy: a restart keeps the service definition.
    assert not CONFIG_RESTART_SERVICES & _deploy_array("APP_SERVICES")


def test_deploy_validates_freeradius_before_database_work_and_restart() -> None:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "freeradius -XC" in script
    # Preflight runs before the first database step.
    assert script.index("if ! preflight_service_config; then") < script.index(
        "\nrun_database_prerequisite_bootstrap\n"
    )
    # The restart path validates first, and the apply step runs only after the
    # application release has been accepted (the ERR rollback trap is off).
    restart = script[script.index("restart_freeradius_with_validation() {") :]
    assert restart.index("validate_freeradius_config") < restart.index(
        '"${COMPOSE[@]}" restart'
    )
    final_trap_reset = script.rindex("\ntrap - ERR\n")
    assert script.index("\napply_service_config_changes\n") > final_trap_reset
