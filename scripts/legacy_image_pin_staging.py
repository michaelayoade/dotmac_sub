#!/usr/bin/python3 -I
"""Atomic staging of the release Compose and its bind variable, together.

WHY THIS IS ONE OPERATION
=========================

The release publishes ``${PG_LOCAL_BIND:-127.0.0.1:}9001:5432``.  Production's
``.env`` does not set ``PG_LOCAL_BIND``.  So a host carrying the release Compose
WITHOUT that variable resolves the publish to loopback, and the next recreate of
``postgres-local`` -- this operation's, a deploy's, or a bare ``docker compose
up`` -- strands the replication standby on a port it is actively streaming WAL
through.  The file and the variable are therefore one change.  Neither may land
alone.

Two files cannot be renamed in a single atomic step, so the pairing is made
atomic with a journal and an explicit COMMIT POINT.

    preparing  -> nothing is committed.  Both originals are preserved and
                  ``recover`` restores them atomically, leaving the host as it
                  was observed.

    committed  -> the commit point has passed.  Recovery NEVER goes backwards
                  from here; it recreates forward with the retained immutable
                  pin and the IPv4-only bind.  Returning to the dual-family
                  publish is break-glass: separately authorized, never
                  automatic, and deliberately not reachable from anything left
                  on disk.

This program only writes files.  It never recreates a container, and
``confirm-no-recreate`` proves that by comparing every container ID across the
operation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

STATE_ROOT = Path("/var/lib/dotmac/legacy-image-pin")
JOURNAL = STATE_ROOT / "staging-journal.json"
PRESERVED = STATE_ROOT / "preserved"
BIND_KEY = "PG_LOCAL_BIND"
DESIRED_BIND = "0.0.0.0:"  # noqa: S104 - declared, source-restricted
SAFE_ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/root",
    "DOCKER_HOST": "unix:///var/run/docker.sock",
    "COMPOSE_ANSI": "never",
}


class StagingError(RuntimeError):
    pass


def _fail(message: str) -> Never:
    raise StagingError(message)


def _canonical(document: dict[str, object]) -> bytes:
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("utf-8")


def _digest_bytes(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _digest_file(path: Path) -> str:
    return _digest_bytes(path.read_bytes())


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _atomic_replace(path: Path, payload: bytes, mode: int, uid: int, gid: int) -> None:
    temporary = path.with_name(f".{path.name}.staging-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, uid, gid)
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_journal(document: dict[str, object]) -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_ROOT, 0o700)
    # Owned by whoever runs this -- root on the host. Hardcoding uid 0 would
    # assert a privilege rather than use one, and would fail anywhere else for
    # a reason that has nothing to do with the operation.
    _atomic_replace(JOURNAL, _canonical(document), 0o600, os.geteuid(), os.getegid())


def _read_journal() -> dict[str, object]:
    raw = JOURNAL.read_bytes()
    document = json.loads(raw)
    if not isinstance(document, dict):
        _fail("staging journal is not an object")
    if raw != _canonical(document):
        _fail("staging journal is not canonical")
    if document.get("schema") != "LegacyImagePinStagingJournalV1":
        _fail("unsupported staging journal schema")
    if document.get("state") not in {"preparing", "committed"}:
        _fail("unknown staging journal state")
    return document


def _container_map(docker_bin: str) -> list[dict[str, str]]:
    ids = subprocess.run(
        [
            docker_bin,
            "ps",
            "-q",
            "--no-trunc",
            "--filter",
            "label=com.docker.compose.project=dotmac_sub",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=SAFE_ENV,
    ).stdout.split()
    if not ids:
        _fail("the Compose project has no running containers")
    rows = subprocess.run(
        [
            docker_bin,
            "inspect",
            *ids,
            "--format",
            '{"service":{{json (index .Config.Labels "com.docker.compose.service")}},"container":{{json .Name}},"container_id":{{json .Id}}}',
        ],
        check=True,
        capture_output=True,
        text=True,
        env=SAFE_ENV,
    ).stdout.splitlines()
    out = [
        {
            "service": json.loads(row)["service"],
            "container": str(json.loads(row)["container"]).lstrip("/"),
            "container_id": str(json.loads(row)["container_id"]).removeprefix(
                "sha256:"
            ),
        }
        for row in rows
        if row.strip()
    ]
    out.sort(key=lambda r: (r["service"], r["container"], r["container_id"]))
    return out


def _env_with_bind(text: str) -> str:
    retained = [
        line for line in text.splitlines() if not line.startswith(f"{BIND_KEY}=")
    ]
    retained.append(f"{BIND_KEY}={DESIRED_BIND}")
    return "\n".join(retained) + "\n"


def stage(
    *,
    compose_path: Path,
    env_path: Path,
    release: Path,
    source_sha: str,
    docker_bin: str,
) -> None:
    """Land both, or neither. The commit point is a single journal write."""

    if JOURNAL.exists():
        _fail("a staging journal already exists; run recover or status first")
    for path in (compose_path, env_path, release):
        if not path.is_file():
            _fail(f"{path} is not a file")

    observed_compose = compose_path.read_bytes()
    observed_env = env_path.read_bytes()
    desired_compose = release.read_bytes()
    desired_env = _env_with_bind(observed_env.decode("utf-8")).encode("utf-8")

    journal = {
        "schema": "LegacyImagePinStagingJournalV1",
        "target_server_name": "dotmac-sub-prod",
        "service": "postgres-local",
        "source_sha": source_sha,
        "compose_path": str(compose_path),
        "env_path": str(env_path),
        "observed_compose_digest": _digest_bytes(observed_compose),
        "observed_env_digest": _digest_bytes(observed_env),
        "desired_compose_digest": _digest_bytes(desired_compose),
        "bind_env": BIND_KEY,
        "desired_bind": DESIRED_BIND,
        "container_ids_before": _container_map(docker_bin),
        "state": "preparing",
        "committed_at": None,
        "updated_at": _now(),
    }
    if journal["observed_compose_digest"] == journal["desired_compose_digest"]:
        _fail("the host already carries the desired release Compose")

    # Preserve both originals BEFORE either is touched. While the journal says
    # "preparing" these are the only way back, and they are removed only once
    # the commit point has passed.
    PRESERVED.mkdir(parents=True, exist_ok=True)
    os.chmod(PRESERVED, 0o700)
    _atomic_replace(
        PRESERVED / "compose.observed",
        observed_compose,
        0o600,
        os.geteuid(),
        os.getegid(),
    )
    _atomic_replace(
        PRESERVED / "env.observed", observed_env, 0o600, os.geteuid(), os.getegid()
    )
    _write_journal(journal)

    compose_stat = compose_path.stat()
    env_stat = env_path.stat()
    _atomic_replace(
        compose_path,
        desired_compose,
        compose_stat.st_mode & 0o777,
        compose_stat.st_uid,
        compose_stat.st_gid,
    )
    _atomic_replace(
        env_path,
        desired_env,
        env_stat.st_mode & 0o777,
        env_stat.st_uid,
        env_stat.st_gid,
    )

    # ---- THE COMMIT POINT ----------------------------------------------
    # Both files are on disk and fsynced. This single journal write is the
    # boundary: before it, recovery restores the observed files; after it,
    # recovery only ever recreates forward.
    journal["state"] = "committed"
    journal["committed_at"] = _now()
    journal["updated_at"] = journal["committed_at"]
    _write_journal(journal)

    # The way back is deliberately destroyed once it must never be taken.
    for name in ("compose.observed", "env.observed"):
        target = PRESERVED / name
        if target.exists():
            target.unlink()


def recover() -> None:
    """Before the commit point: restore both observed files, atomically."""

    journal = _read_journal()
    if journal["state"] == "committed":
        _fail(
            "staging is past its commit point; recovery from here recreates "
            "FORWARD with the retained pin and the IPv4-only bind. Returning to "
            "the dual-family publish is break-glass and is not automatic."
        )
    compose_path = Path(str(journal["compose_path"]))
    env_path = Path(str(journal["env_path"]))
    compose_backup = PRESERVED / "compose.observed"
    env_backup = PRESERVED / "env.observed"
    if not compose_backup.is_file() or not env_backup.is_file():
        _fail("the preserved originals are missing; refusing to guess")
    for backup, path, digest_key in (
        (compose_backup, compose_path, "observed_compose_digest"),
        (env_backup, env_path, "observed_env_digest"),
    ):
        if _digest_file(backup) != journal[digest_key]:
            _fail(f"preserved {path.name} does not match the observed digest")
    for backup, path in ((compose_backup, compose_path), (env_backup, env_path)):
        stat = path.stat()
        _atomic_replace(
            path, backup.read_bytes(), stat.st_mode & 0o777, stat.st_uid, stat.st_gid
        )
    JOURNAL.unlink()
    for backup in (compose_backup, env_backup):
        backup.unlink()


def confirm_no_recreate(docker_bin: str) -> None:
    """Staging writes files; it must not have recreated anything."""

    journal = _read_journal()
    before = [
        {
            "service": row["service"],
            "container": row["container"],
            "container_id": row["container_id"],
        }
        for row in journal["container_ids_before"]
    ]
    after = _container_map(docker_bin)
    if after != before:
        _fail(
            "staging changed a container identity; it writes files and must "
            "recreate nothing"
        )


def status() -> None:
    if not JOURNAL.exists():
        os.write(sys.stdout.fileno(), b"no staging journal\n")
        return
    journal = _read_journal()
    os.write(
        sys.stdout.fileno(),
        f"staging state: {journal['state']}\n".encode(),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("stage")
    run.add_argument("--compose", required=True, type=Path)
    run.add_argument("--env-file", required=True, type=Path)
    run.add_argument("--release-compose", required=True, type=Path)
    run.add_argument("--source-sha", required=True)
    run.add_argument("--docker-bin", required=True)
    commands.add_parser("recover")
    confirm = commands.add_parser("confirm-no-recreate")
    confirm.add_argument("--docker-bin", required=True)
    commands.add_parser("status")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "stage":
            stage(
                compose_path=args.compose,
                env_path=args.env_file,
                release=args.release_compose,
                source_sha=args.source_sha,
                docker_bin=args.docker_bin,
            )
        elif args.command == "recover":
            recover()
        elif args.command == "confirm-no-recreate":
            confirm_no_recreate(args.docker_bin)
        elif args.command == "status":
            status()
    except (StagingError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"LEGACY IMAGE PIN STAGING FAILED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
