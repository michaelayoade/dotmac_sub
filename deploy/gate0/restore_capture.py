"""Private controlled capture adapter around the existing recovery bundle owner.

Catalog locking is deliberately fail closed. Its PostgreSQL 16 dedicated-host
acceptance packet must pass before operational use; offline tests prove no such
acceptance. No credentials or database rows enter the public evidence contract.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast

from deploy.agent import contracts as c
from deploy.gate0.evidence import GateContext
from scripts.restore_drill import (
    inventory_digest,
    load_bundle,
    sha256_file,
    verify_hashes,
)
from scripts.restore_guard import (
    HostPurpose,
    RestoreRefusal,
    enrolled,
    protected,
    unique_object,
)
from scripts.restore_inventory import INVENTORY_SQL, parse_inventory

READ_TIMEOUT_SECONDS = 40
MAX_RECORD_BYTES = 64 * 1024 * 1024
SOURCE = "hp-server/dotmac_sub_db/dotmac_sub"
SAFE_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C"}
DOCKER = ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
PSQL = [
    *DOCKER,
    "exec",
    "-i",
    "dotmac_sub_db",
    "psql",
    "-XqAt",
    "-U",
    "postgres",
    "-d",
    "dotmac_sub",
    "-v",
    "ON_ERROR_STOP=1",
]
# Lock every heap catalog, including shared role/membership/database catalogs.
# A SHARE lock conflicts with RowExclusive catalog writes while permitting dump
# reads. Lock the migration-head data table too: migration DML is not DDL.
FREEZE_SQL = """
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='30s';
SET LOCAL idle_in_transaction_session_timeout='60min';
DO $freeze$ DECLARE r record; BEGIN
 FOR r IN SELECT n.nspname,c.relname FROM pg_class c
 JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='pg_catalog' AND c.relkind IN ('r','p') ORDER BY c.oid
 LOOP EXECUTE format('LOCK TABLE %I.%I IN SHARE MODE',r.nspname,r.relname); END LOOP;
END $freeze$;
LOCK TABLE public.alembic_version IN SHARE MODE;
"""
OBSERVE_SQL = """
SELECT json_build_object(
 'pid',pg_backend_pid(),
 'required',(SELECT json_agg(c.oid ORDER BY c.oid) FROM pg_class c
 JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE (n.nspname='pg_catalog' AND c.relkind IN ('r','p'))
 OR c.oid='public.alembic_version'::regclass),
 'held',(SELECT json_agg(relation ORDER BY relation) FROM pg_locks
 WHERE pid=pg_backend_pid() AND locktype='relation' AND mode='ShareLock' AND granted));
"""


class CapturePlanDocument(TypedDict):
    context: GateContext
    observation_not_before: str
    source_identity: str
    test_host_identity: str
    owner_sha256: c.Digest
    library_sha256: c.Digest
    source_permit_sha256: c.Digest
    restore_permit_sha256: c.Digest
    capture_tool_sha256: c.Digest
    restore_tool_sha256: c.Digest
    image: str
    capture_sha256: c.Digest | None


@dataclass(frozen=True)
class CapturePlan:
    context: GateContext
    observation_not_before: str
    source_identity: str
    test_host_identity: str
    owner_sha256: str
    library_sha256: str
    source_permit_sha256: str
    restore_permit_sha256: str
    capture_tool_sha256: str
    restore_tool_sha256: str
    image: str
    capture_sha256: str | None

    @classmethod
    def load(cls, path: Path) -> CapturePlan:
        protected(path)
        try:
            if path.stat().st_size > 16384:
                raise ValueError
            raw = json.loads(path.read_text(), object_pairs_hook=unique_object)
            c._shape(raw, CapturePlanDocument)
            value = cast(CapturePlanDocument, raw)
            plan = cls(**value)
            context = plan.context
            c.require(context["run_id"] != context["prior_run_id"])
            c.require(
                (context["phase"] == "initial") == (context["prior_run_id"] is None)
            )
            c.require(
                1 <= len(context["action_refs"]) <= 16
                and len(set(context["action_refs"])) == len(context["action_refs"])
            )
            c.require(
                c.timestamp(plan.observation_not_before)
                < c.timestamp(context["deadline"])
            )
            c.require(
                plan.source_identity == SOURCE
                and plan.test_host_identity == "vmi3537655"
            )
            c.require(
                re.fullmatch(
                    r"(?:[a-z0-9.-]+(?::[0-9]+)?/)?[a-z0-9_./-]+@sha256:[0-9a-f]{64}",
                    plan.image,
                )
                is not None
            )
            return plan
        except (ValueError, TypeError, KeyError):
            raise RestoreRefusal("capture_plan_invalid") from None

    def window(self, started: str, finished: str) -> None:
        try:
            start, finish = c.timestamp(started), c.timestamp(finished)
            c.require(
                c.timestamp(self.observation_not_before)
                <= start
                <= finish
                <= c.timestamp(self.context["deadline"])
            )
            c.require(finish <= datetime.now(UTC))
        except (ValueError, TypeError):
            raise RestoreRefusal("capture_window_invalid") from None


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def local_git_identity(root: Path) -> tuple[str, str]:
    """Observe a clean local checkout; never consult network or credentials."""
    protected(Path("/usr/bin/git"))
    command = [
        "/usr/bin/git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-C",
        str(root),
    ]
    results = [
        subprocess.run(
            [*command, *args],
            capture_output=True,
            text=True,
            env=SAFE_ENV,
            timeout=15,
            check=False,
        )
        for args in (
            ("rev-parse", "HEAD"),
            ("rev-parse", "HEAD^{tree}"),
            ("status", "--porcelain", "--untracked-files=all"),
        )
    ]
    if any(result.returncode for result in results) or results[2].stdout.strip():
        raise RestoreRefusal("capture_checkout_unproved")
    head, tree = (result.stdout.strip() for result in results[:2])
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (head, tree)):
        raise RestoreRefusal("capture_checkout_unproved")
    return head, tree


def pinned_file(path: Path, digest: str) -> None:
    protected(path)
    if "sha256:" + sha256_file(path) != digest:
        raise RestoreRefusal("capture_tool_unpinned")


def verify_tools(plan: CapturePlan) -> str:
    tool_root = Path(__file__).resolve().parents[2]
    pinned_file(tool_root / "deploy/gate0/restore_capture.py", plan.capture_tool_sha256)
    pinned_file(tool_root / "deploy/gate0/proof5_restore.py", plan.restore_tool_sha256)
    revision, _ = local_git_identity(tool_root)
    if revision != plan.context["tool_revision"]:
        raise RestoreRefusal("capture_tool_revision")
    return revision


@dataclass(frozen=True)
class FreezeObservation:
    pid: int
    relations: tuple[int, ...]

    @classmethod
    def parse(cls, raw: str) -> FreezeObservation:
        from scripts.restore_guard import unique_object

        try:
            value = json.loads(raw, object_pairs_hook=unique_object)
            if not isinstance(value, dict) or set(value) != {"pid", "required", "held"}:
                raise ValueError
            required, held = value["required"], value["held"]
            if (
                type(value["pid"]) is not int
                or value["pid"] <= 0
                or not isinstance(required, list)
                or not isinstance(held, list)
            ):
                raise ValueError
            if (
                len(required) < 20
                or any(type(x) is not int or x <= 0 for x in required + held)
                or len(set(required)) != len(required)
                or sorted(required) != sorted(held)
            ):
                raise ValueError
            return cls(value["pid"], tuple(sorted(required)))
        except (ValueError, KeyError, TypeError):
            raise RestoreRefusal("capture_lock_observation") from None


class CatalogFreeze:
    def __enter__(self) -> CatalogFreeze:
        enrolled(HostPurpose.CAPTURE)
        self.process = subprocess.Popen(
            PSQL,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=SAFE_ENV,
            text=True,
            bufsize=1,
        )
        try:
            self.write(FREEZE_SQL)
            self.before = self.observe()
        except BaseException:
            self.close()
            raise
        return self

    def write(self, sql: str) -> None:
        if self.process.poll() is not None or self.process.stdin is None:
            raise RestoreRefusal("capture_freeze_lost")
        try:
            self.process.stdin.write(sql + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError):
            raise RestoreRefusal("capture_freeze_lost") from None

    def scalar(self, sql_text: str) -> str:
        self.write(sql_text)
        if self.process.stdout is None:
            raise RestoreRefusal("capture_freeze_lost")
        descriptor = self.process.stdout.fileno()
        os.set_blocking(descriptor, False)
        buffered = getattr(self, "_buffered", bytearray())
        self._buffered = buffered
        deadline = time.monotonic() + READ_TIMEOUT_SECONDS
        with selectors.DefaultSelector() as selector:
            selector.register(descriptor, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RestoreRefusal("capture_query_timeout")
                newline = buffered.find(b"\n")
                if newline >= 0:
                    if newline > MAX_RECORD_BYTES:
                        raise RestoreRefusal("capture_record_oversized")
                    record = bytes(buffered[:newline])
                    del buffered[: newline + 1]
                    try:
                        result = record.decode("utf-8").strip()
                    except UnicodeError:
                        raise RestoreRefusal("capture_record_invalid") from None
                    if not result or self.process.poll() is not None:
                        raise RestoreRefusal("capture_freeze_lost")
                    return result
                if len(buffered) > MAX_RECORD_BYTES:
                    raise RestoreRefusal("capture_record_oversized")
                if not selector.select(timeout=remaining):
                    raise RestoreRefusal("capture_query_timeout")
                try:
                    chunk = os.read(descriptor, min(65536, MAX_RECORD_BYTES + 1))
                except BlockingIOError:
                    continue
                if not chunk:
                    raise RestoreRefusal("capture_freeze_lost")
                buffered.extend(chunk)

    def observe(self) -> FreezeObservation:
        return FreezeObservation.parse(self.scalar(OBSERVE_SQL))

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.write("ROLLBACK;\\q")
                self.process.communicate(timeout=10)
            except (OSError, ValueError, subprocess.TimeoutExpired, RestoreRefusal):
                self.process.kill()
                self.process.communicate(timeout=10)
        before = getattr(self, "before", None)
        if isinstance(before, FreezeObservation):
            enrolled(HostPurpose.CAPTURE)
            absence = subprocess.run(
                [*PSQL, "-v", "capture_pid=" + str(before.pid)],
                input="SELECT NOT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid=:'capture_pid'::integer);\n",
                capture_output=True,
                text=True,
                env=SAFE_ENV,
                timeout=15,
                check=False,
            )
            if absence.returncode or absence.stdout.strip() != "t":
                raise RestoreRefusal("capture_cleanup_unproved")
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe is not None:
                pipe.close()

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass(frozen=True)
class RestoreCapture:
    source_identity: str
    started_at: str
    finished_at: str
    bundle_sha256: str
    inventory_sha256: str
    inventory_file_sha256: str
    owner_sha256: str
    permit_sha256: str
    freeze: FreezeObservation
    context: GateContext
    source_revision: str
    source_tree: str
    tool_revision: str
    library_sha256: str
    capture_tool_sha256: str
    restore_tool_sha256: str


def capture(root: Path, private_parent: Path, plan_path: Path) -> Path:
    """Run only on enrolled source; return protected private capture directory."""
    enrolled(HostPurpose.CAPTURE)
    plan = CapturePlan.load(plan_path)
    if plan.capture_sha256 is not None:
        raise RestoreRefusal("capture_plan_phase")
    plan.window(utc_now(), utc_now())
    pinned_file(Path("/etc/dotmac/gate0-host.json"), plan.source_permit_sha256)
    tool_revision = verify_tools(plan)
    source_revision, source_tree = local_git_identity(root)
    if (source_revision, source_tree) != (
        plan.context["source_revision"],
        plan.context["source_tree"],
    ):
        raise RestoreRefusal("capture_source_revision")
    owner = root / "scripts/db_backup.sh"
    library = root / "scripts/backup/recovery_bundle_lib.sh"
    pinned_file(owner, plan.owner_sha256)
    pinned_file(library, plan.library_sha256)
    for path in (owner, root / "scripts/backup/recovery_bundle_lib.sh", root / ".env"):
        protected(path)
    # Parent must already be provisioned root-only; never use shared retention.
    protected_directory(private_parent)
    directory = Path(tempfile.mkdtemp(prefix="gate0-capture-", dir=private_parent))
    directory.chmod(0o700)
    tools_dir = directory / "bin"
    tools_dir.mkdir(mode=0o700)
    wrapper = tools_dir / "docker"
    wrapper.write_text(
        '#!/bin/sh\nexec /usr/bin/env -i PATH=/usr/bin:/bin LANG=C /usr/bin/docker --host unix:///var/run/docker.sock "$@"\n'
    )
    wrapper.chmod(0o700)
    env = {
        **SAFE_ENV,
        "PATH": f"{tools_dir}:/usr/bin:/bin",
        "ROOT_DIR": str(root),
        "DB_CONTAINER": "dotmac_sub_db",
        "DB_BACKUP_DIR": str(directory),
        "DB_BACKUP_ROOT": str(directory),
        "DB_BACKUP_LEGACY_DIR": str(directory),
        "DB_BACKUP_BASENAME": "gate0",
        "DB_BACKUP_RETENTION_PREFIX": "gate0_",
        "DB_BACKUP_RETENTION_COUNT": "1",
        "DB_BACKUP_DB_NAME": "dotmac_sub",
        "DB_BACKUP_DB_USER": "postgres",
        "GIT_SHA": source_revision,
    }
    # All private files, including shell owner artifacts, are root-only.
    prior_umask = os.umask(0o077)
    try:
        with CatalogFreeze() as freeze:
            started = utc_now()
            backup = subprocess.run(
                ["/bin/bash", str(owner)],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1800,
                check=False,
            )
            if backup.returncode:
                raise RestoreRefusal("capture_owner_failed")
            manifests = list(directory.glob("*.manifest.json"))
            if len(manifests) != 1:
                raise RestoreRefusal("capture_bundle_count")
            manifest = manifests[0]
            bundle, artifacts = load_bundle(manifest)
            if (
                bundle.database != "dotmac_sub"
                or bundle.git_sha != source_revision
                or verify_hashes(bundle, artifacts)
            ):
                raise RestoreRefusal("capture_bundle_invalid")
            raw = freeze.scalar(INVENTORY_SQL)
            inventory = parse_inventory(raw)
            inventory_path = directory / "inventory.json"
            inventory_path.write_text(raw + "\n")
            after = freeze.observe()
            if after != freeze.before:
                raise RestoreRefusal("capture_freeze_changed")
            finished = utc_now()
            plan.window(started, finished)
            bundle_time(plan, bundle.created_at, started, finished)
            if (
                local_git_identity(root) != (source_revision, source_tree)
                or verify_tools(plan) != tool_revision
            ):
                raise RestoreRefusal("capture_checkout_changed")
            pinned_file(owner, plan.owner_sha256)
            pinned_file(library, plan.library_sha256)
            record = RestoreCapture(
                SOURCE,
                started,
                finished,
                sha256_file(manifest),
                inventory_digest(inventory),
                sha256_file(inventory_path),
                sha256_file(owner),
                sha256_file(Path("/etc/dotmac/gate0-host.json")),
                after,
                plan.context,
                source_revision,
                source_tree,
                tool_revision,
                sha256_file(library),
                plan.capture_tool_sha256.removeprefix("sha256:"),
                plan.restore_tool_sha256.removeprefix("sha256:"),
            )
        (directory / "capture.json").write_text(
            json.dumps(asdict(record), sort_keys=True) + "\n"
        )
        return directory
    finally:
        os.umask(prior_umask)


def bundle_time(
    plan: CapturePlan, created_at: str, started: str, finished: str
) -> None:
    try:
        created = c.timestamp(created_at)
        first = c.timestamp(started).replace(microsecond=0)
        last = c.timestamp(finished)
        c.require(first <= created <= last)
        c.require(
            c.timestamp(plan.observation_not_before).replace(microsecond=0)
            <= created
            <= c.timestamp(plan.context["deadline"])
        )
    except (ValueError, TypeError):
        raise RestoreRefusal("capture_bundle_time") from None


def protected_directory(path: Path) -> None:
    # protected() validates ancestry too, but is deliberately file-specific.
    import stat

    for entry in (path, *path.parents):
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RestoreRefusal("capture_directory_unprotected")
    if not path.is_dir() or path.stat().st_mode & 0o077:
        raise RestoreRefusal("capture_directory_unprotected")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--private-parent", type=Path, required=True)
    args = parser.parse_args()
    try:
        capture(args.root, args.private_parent, args.plan)
        print("PRIVATE_CAPTURE_COMPLETE")
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        print("PRIVATE_CAPTURE_REFUSED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
