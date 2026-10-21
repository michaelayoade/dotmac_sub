#!/usr/bin/env python3
"""Restore drill: prove a recovery bundle restores WITH its isolation model.

Restores a bundle (globals + database dump + manifest, produced by
``scripts/db_backup.sh`` or ``scripts/backup/backup_dotmac_sub_dbs_to_rclone.sh``;
format in ``docs/RECOVERY_BUNDLE.md``) into a DISPOSABLE scratch PostgreSQL
and asserts the tenant-isolation model came back: roles and postures, FORCEd
RLS, the cross-plane revocation, cross-tenant invisibility, and ownership
that is not collapsed onto the restoring superuser.

The scratch target is guarded hard: generated ``dotmac-restore-drill-*``
container name, ``--network none``, tmpfs data dir, no published ports, a
fixed scratch database name, and refusal of any user-supplied target that
resembles production. Nothing here can be pointed at a live database.

Usage::

    python scripts/restore_drill.py --manifest /path/to/<stem>.manifest.json
    python scripts/restore_drill.py --manifest ... --dry-run   # no docker
    python scripts/restore_drill.py --self-test               # synthetic CI fixture

Exit codes: 0 all assertions passed, 1 an assertion failed, 2 usage/guard
refusal, 3 infrastructure failure (docker/postgres unavailable). The verdict
is always one JSON document on stdout (and ``--out`` if given).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

if __package__:
    from .restore_inventory import Inventory, collect_inventory, first_divergence
else:
    from restore_inventory import Inventory, collect_inventory, first_divergence

EXIT_OK = 0
EXIT_ASSERTION = 1
EXIT_USAGE = 2
EXIT_INFRA = 3

SCRATCH_DB = "restore_drill_scratch"
SCRATCH_PREFIX = "dotmac-restore-drill-"
DEFAULT_IMAGE = "postgres:16-alpine"

#: Names that must never be a drill target, as substrings (case-insensitive).
#: Production containers/DBs across the fleet plus generic prod markers.
FORBIDDEN_TARGET_FRAGMENTS = (
    "prod",
    "dotmac_pg_local",
    "dotmac_sub_db",
    "dotmac_sub_radius_db",
    "dotmac_sub_app",
)

#: Fallback mirror of the checked-in role contracts, used only when the app
#: packages cannot be imported (bare CI image). The sources of truth are
#: app/commercial_module_prereqs.py (COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT +
#: SCHEMA_BOOTSTRAP_ROLE) and app/outbox_dispatcher_roles.py
#: (RELAY_DISPATCHER_CONTRACT), created by
#: scripts/bootstrap_commercial_module_prereqs.py and
#: scripts/bootstrap_outbox_dispatcher_roles.py and verified by migration
#: 557_outbox_relay_prereq.py. Posture: (rolcanlogin, rolbypassrls, rolsuper).
_FALLBACK_ROLE_CONTRACT: dict[str, tuple[bool, bool, bool]] = {
    "dotmac_app": (True, False, False),
    "app_admin": (True, True, False),
    "app_user": (True, False, False),
    "platform_api": (True, False, False),
    "dotmac_public_probe": (False, False, False),
    "outbox_dispatcher": (True, False, False),
    "platform_outbox_dispatcher": (True, False, False),
}
#: Provisioned out of band; presence asserted, posture owned by the runbook.
_FALLBACK_PRESENCE_ONLY = ("dotmac_schema_bootstrap",)


def expected_role_contract() -> tuple[
    dict[str, tuple[bool, bool, bool]], tuple[str, ...]
]:
    """Derive expected roles from the role-creation sites' contracts."""

    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from app.commercial_module_prereqs import (
            COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT,
            SCHEMA_BOOTSTRAP_ROLE,
        )
        from app.outbox_dispatcher_roles import RELAY_DISPATCHER_CONTRACT

        contract = {
            name: role.posture
            for name, role in COMMERCIAL_BOOTSTRAP_ROLE_CONTRACT.items()
        }
        contract.update(dict(RELAY_DISPATCHER_CONTRACT))
        return contract, (SCHEMA_BOOTSTRAP_ROLE,)
    except Exception:  # bare environment: mirror above is the fallback
        return dict(_FALLBACK_ROLE_CONTRACT), _FALLBACK_PRESENCE_ONLY


def refuse_target(name: str, *, what: str) -> str | None:
    """Return a refusal reason if a target name resembles production."""

    lowered = name.lower()
    for fragment in FORBIDDEN_TARGET_FRAGMENTS:
        if fragment in lowered:
            return f"{what} {name!r} resembles production (contains {fragment!r})"
    if lowered in {"dotmac_sub", "radius", "postgres"} and what == "database":
        return f"{what} {name!r} is a live database name, not a scratch target"
    return None


def scratch_container_name() -> str:
    return f"{SCRATCH_PREFIX}{uuid.uuid4().hex[:12]}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_bundle(manifest_path: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    """Load and shape-check a bundle manifest; resolve artifact paths."""

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "dotmac_sub.recovery_bundle.v1":
        raise ValueError(f"unsupported bundle schema: {manifest.get('schema')!r}")
    artifacts: dict[str, Path] = {}
    for entry in manifest.get("artifacts", []):
        artifacts[str(entry["role"])] = manifest_path.parent / str(entry["file"])
    for required in ("globals", "database_dump"):
        if required not in artifacts:
            raise ValueError(f"manifest is missing the {required!r} artifact")
        if not artifacts[required].is_file():
            raise ValueError(f"artifact file not found: {artifacts[required]}")
    return manifest, artifacts


def verify_hashes(manifest: dict[str, Any], artifacts: dict[str, Path]) -> list[str]:
    failures = []
    for entry in manifest.get("artifacts", []):
        role = str(entry["role"])
        actual = sha256_file(artifacts[role])
        if actual != entry.get("sha256"):
            failures.append(
                f"{role}: sha256 mismatch (manifest {entry.get('sha256')}, actual {actual})"
            )
    return failures


_ROLE_RE = re.compile(
    r'^(?:CREATE|ALTER) ROLE (?:"([^"]+)"|([A-Za-z0-9_]+))', re.MULTILINE
)
_PASSWORD_RE = re.compile(r"PASSWORD '(SCRAM-SHA-256\$|md5[0-9a-f]{32})")


def parse_globals_roles(globals_path: Path) -> tuple[set[str], bool]:
    """Return (roles defined in globals, contains password verifiers)."""

    with gzip.open(globals_path, "rt", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    roles = {(m.group(1) or m.group(2)) for m in _ROLE_RE.finditer(text)}
    return roles, bool(_PASSWORD_RE.search(text))


def run(
    cmd: list[str], *, input_bytes: bytes | None = None, timeout: int = 600
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        cmd, input=input_bytes, capture_output=True, timeout=timeout, check=False
    )


class ScratchPostgres:
    """One disposable, network-isolated PostgreSQL container."""

    def __init__(self, image: str) -> None:
        self.image = image
        self.name = scratch_container_name()
        self._ownership_label = uuid.uuid4().hex
        refusal = refuse_target(self.name, what="container")
        if refusal:  # generated name must always pass; guard anyway
            raise RuntimeError(refusal)

    def __enter__(self) -> ScratchPostgres:
        try:
            return self._start()
        except BaseException:
            self._cleanup()
            raise

    def _start(self) -> ScratchPostgres:
        result = run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self.name,
                "--label",
                f"dotmac.restore-drill.run={self._ownership_label}",
                "--network",
                "none",
                "--tmpfs",
                "/var/lib/postgresql/data",
                "-e",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "-e",
                f"POSTGRES_DB={SCRATCH_DB}",
                self.image,
            ],
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError("scratch container startup failed")
        deadline = time.time() + 90
        while time.time() < deadline:
            probe = self.psql("SELECT 1", db=SCRATCH_DB, tuples_only=True)
            if probe.returncode == 0 and probe.stdout.strip() == b"1":
                return self
            time.sleep(1)
        raise RuntimeError("scratch postgres did not become ready")

    def __exit__(self, *exc: object) -> None:
        self._cleanup()

    def _container_record(self) -> str:
        try:
            result = run(
                [
                    "docker",
                    "container",
                    "ls",
                    "--all",
                    "--no-trunc",
                    "--filter",
                    f"name=^/{self.name}$",
                    "--format",
                    '{{.ID}}|{{.Label "dotmac.restore-drill.run"}}',
                ],
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError("scratch presence query failed") from None
        if result.returncode != 0:
            raise RuntimeError("scratch presence query failed")
        return result.stdout.decode().strip()

    def _cleanup(self) -> None:
        record = self._container_record()
        if not record:
            return
        container_id, separator, label = record.partition("|")
        if (
            not separator
            or not re.fullmatch(r"[0-9a-f]{64}", container_id)
            or label != self._ownership_label
        ):
            raise RuntimeError("scratch ownership unproved; removal refused")
        removed = False
        try:
            removed = (
                run(["docker", "rm", "-f", container_id], timeout=60).returncode == 0
            )
        except (OSError, subprocess.SubprocessError):
            pass
        absent = not self._container_record()
        if not removed or not absent:
            raise RuntimeError("scratch cleanup failed")

    def psql(
        self,
        sql_text: str,
        *,
        db: str,
        tuples_only: bool = False,
        on_error_stop: bool = True,
        timeout: int = 600,
    ) -> subprocess.CompletedProcess[bytes]:
        cmd = [
            "docker",
            "exec",
            "-i",
            self.name,
            "psql",
            "-U",
            "postgres",
            "-d",
            db,
            "-v",
            f"ON_ERROR_STOP={1 if on_error_stop else 0}",
        ]
        if tuples_only:
            cmd += ["-At", "-F", "|"]
        return run(cmd, input_bytes=sql_text.encode(), timeout=timeout)

    def scalar(self, sql_text: str, *, db: str = SCRATCH_DB) -> str:
        result = self.psql(sql_text, db=db, tuples_only=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"query failed: {result.stderr.decode(errors='replace')[:500]}"
            )
        return result.stdout.decode().strip()


def _check(name: str, passed: bool, detail: str, checks: list[dict[str, Any]]) -> None:
    checks.append({"name": name, "passed": passed, "detail": detail})


def assert_isolation_model(
    pg: ScratchPostgres, *, globals_roles: set[str]
) -> list[dict[str, Any]]:
    """Assert roles, RLS, revocation, cross-tenant and ownership invariants."""

    checks: list[dict[str, Any]] = []
    contract, presence_only = expected_role_contract()

    rows = pg.scalar(
        "SELECT rolname || '|' || rolcanlogin || '|' || rolbypassrls || '|' || rolsuper FROM pg_roles"
    )
    observed: dict[str, tuple[bool, bool, bool]] = {}
    for line in rows.splitlines():
        if not line.strip():
            continue
        name, login, bypass, superuser = line.split("|")
        observed[name] = (login == "t", bypass == "t", superuser == "t")

    # 1. Every non-builtin role the globals artifact defines must now exist.
    expected_from_globals = {r for r in globals_roles if not r.startswith("pg_")}
    missing = sorted(r for r in expected_from_globals if r not in observed)
    _check(
        "globals_roles_restored",
        not missing,
        f"{len(expected_from_globals)} roles in globals; missing: {missing or 'none'}",
        checks,
    )

    # 2. Contract roles (role-creation sites) present with exact posture.
    wrong = [
        f"{role}: observed {observed.get(role)}, expected {posture}"
        for role, posture in sorted(contract.items())
        if role in expected_from_globals and observed.get(role) != posture
    ]
    absent_contract = sorted(r for r in contract if r not in observed)
    _check(
        "contract_role_postures",
        not wrong and not absent_contract,
        f"absent: {absent_contract or 'none'}; wrong posture: {wrong or 'none'}",
        checks,
    )
    absent_presence = sorted(r for r in presence_only if r not in observed)
    _check(
        "presence_only_roles",
        not absent_presence,
        f"missing: {absent_presence or 'none'}",
        checks,
    )

    # 3. RLS: enabled tables exist, every policy-backed table is FORCEd.
    rls = pg.scalar(
        "SELECT count(*) FILTER (WHERE relrowsecurity) || '|' || "
        "count(*) FILTER (WHERE relrowsecurity AND NOT relforcerowsecurity) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog','information_schema')"
    )
    enabled, unforced = (int(part) for part in rls.split("|"))
    _check("rls_enabled_present", enabled > 0, f"RLS-enabled tables: {enabled}", checks)
    _check(
        "rls_forced", unforced == 0, f"RLS-enabled but not FORCEd: {unforced}", checks
    )
    orphan_policies = int(
        pg.scalar(
            "SELECT count(*) FROM pg_policies p JOIN pg_class c ON c.relname = p.tablename "
            "JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = p.schemaname "
            "WHERE NOT c.relrowsecurity"
        )
    )
    _check(
        "no_policies_without_rls",
        orphan_policies == 0,
        f"policies on tables with RLS disabled: {orphan_policies}",
        checks,
    )

    # 4. Cross-plane revocation: app_user must be denied platform_outbox_events
    #    (migration 557 REVOKE). Skipped only when the plane is absent.
    has_plane = pg.scalar(
        "SELECT to_regclass('public.platform_outbox_events') IS NOT NULL"
    )
    if has_plane == "t" and "app_user" in observed:
        probe = pg.psql(
            "SET ROLE app_user; SELECT count(*) FROM public.platform_outbox_events;",
            db=SCRATCH_DB,
            tuples_only=True,
        )
        denied = probe.returncode != 0 and b"permission denied" in probe.stderr.lower()
        _check(
            "cross_plane_revocation",
            denied,
            "app_user SELECT on platform_outbox_events denied"
            if denied
            else f"app_user was NOT denied (rc={probe.returncode})",
            checks,
        )
    else:
        _check(
            "cross_plane_revocation",
            True,
            "platform plane or app_user absent; probe not applicable",
            checks,
        )

    # 5. Cross-tenant invisibility: as app_user under a random tenant, the
    #    tenant relay plane must show zero rows (RLS actually evaluating).
    has_tenant_plane = pg.scalar(
        "SELECT to_regclass('public.outbox_events') IS NOT NULL"
    )
    if has_tenant_plane == "t" and "app_user" in observed:
        probe = pg.psql(
            "SET ROLE app_user; "
            f"SELECT set_config('app.current_tenant', '{uuid.uuid4()}', false); "
            "SELECT count(*) FROM public.outbox_events;",
            db=SCRATCH_DB,
            tuples_only=True,
        )
        visible = (
            probe.stdout.decode().strip().splitlines()[-1]
            if probe.stdout.strip()
            else "?"
        )
        _check(
            "cross_tenant_invisibility",
            probe.returncode == 0 and visible == "0",
            f"rows visible to foreign tenant: {visible} (rc={probe.returncode})",
            checks,
        )
    else:
        _check(
            "cross_tenant_invisibility",
            True,
            "tenant plane or app_user absent; probe not applicable",
            checks,
        )

    # 6. Ownership not collapsed: the Vendor CP failure shape is every object
    #    owned by the restoring superuser while globals defines app roles.
    owners = pg.scalar(
        "SELECT count(DISTINCT tableowner) || '|' || count(*) FILTER (WHERE tableowner = 'postgres') || '|' || count(*) "
        "FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')"
    )
    distinct_owners, postgres_owned, total_tables = (
        int(part) for part in owners.split("|")
    )
    collapsed = (
        total_tables > 0
        and postgres_owned == total_tables
        and len(expected_from_globals - {"postgres"}) > 0
    )
    _check(
        "ownership_not_collapsed",
        not collapsed,
        f"distinct owners: {distinct_owners}, postgres-owned: {postgres_owned}/{total_tables}",
        checks,
    )
    return checks


def drill_bundle(
    manifest_path: Path, image: str, *, expected_inventory: Inventory | None = None
) -> dict[str, Any]:
    """Restore one bundle into scratch postgres and assert the model."""

    manifest, artifacts = load_bundle(manifest_path)
    checks: list[dict[str, Any]] = []
    hash_failures = verify_hashes(manifest, artifacts)
    _check(
        "manifest_sha256",
        not hash_failures,
        "; ".join(hash_failures) or "all artifacts match manifest",
        checks,
    )
    globals_roles, has_passwords = parse_globals_roles(artifacts["globals"])
    _check(
        "globals_no_password_material",
        not has_passwords,
        "password verifier found in globals"
        if has_passwords
        else "no password verifiers in globals",
        checks,
    )
    _check(
        "globals_defines_roles",
        len(globals_roles) > 1,
        f"roles defined in globals: {sorted(globals_roles)}",
        checks,
    )
    if any(not c["passed"] for c in checks):
        return {"verdict": "FAIL", "stage": "bundle_validation", "checks": checks}

    with ScratchPostgres(image) as pg:
        with gzip.open(artifacts["globals"], "rb") as handle:
            globals_sql = handle.read().decode(errors="replace")
        # Globals reference every database/role in the source cluster; some
        # already exist here. Error-stop off, then assertions adjudicate.
        pg.psql(globals_sql, db=SCRATCH_DB, on_error_stop=False, timeout=900)
        with gzip.open(artifacts["database_dump"], "rb") as handle:
            dump_sql = handle.read().decode(errors="replace")
        restored = pg.psql(dump_sql, db=SCRATCH_DB, on_error_stop=True, timeout=1800)
        _check(
            "database_dump_restores_clean",
            restored.returncode == 0,
            "dump applied with ON_ERROR_STOP=1"
            if restored.returncode == 0
            else "dump failed with ON_ERROR_STOP=1",
            checks,
        )
        if restored.returncode == 0:
            checks.extend(assert_isolation_model(pg, globals_roles=globals_roles))
            if expected_inventory is not None:
                try:
                    divergence = first_divergence(
                        expected_inventory, collect_inventory(pg)
                    )
                    detail = divergence or "all inventoried metadata matches source"
                    matched = divergence is None
                except (RuntimeError, ValueError):
                    matched, detail = False, "metadata collection failed"
                _check("source_metadata_parity", matched, detail, checks)
    passed = all(c["passed"] for c in checks)
    return {
        "verdict": "PASS" if passed else "FAIL",
        "stage": "restore_and_assert",
        "database": manifest.get("database"),
        "bundle_created_at": manifest.get("created_at"),
        "checks": checks,
    }


#: Synthetic fixture mirroring Sub's isolation shape in miniature: every
#: contract role, a FORCEd-RLS tenant plane, a revoked platform plane, and
#: non-superuser ownership. Used by --self-test so CI needs no prod data.
FIXTURE_SQL = """
CREATE ROLE dotmac_app LOGIN NOBYPASSRLS NOSUPERUSER;
CREATE ROLE app_admin LOGIN BYPASSRLS NOSUPERUSER;
CREATE ROLE app_user LOGIN NOBYPASSRLS NOSUPERUSER;
CREATE ROLE platform_api LOGIN NOBYPASSRLS NOSUPERUSER;
CREATE ROLE dotmac_public_probe NOLOGIN NOBYPASSRLS NOSUPERUSER;
CREATE ROLE dotmac_schema_bootstrap LOGIN NOBYPASSRLS NOSUPERUSER;
CREATE ROLE outbox_dispatcher LOGIN NOBYPASSRLS NOSUPERUSER;
CREATE ROLE platform_outbox_dispatcher LOGIN NOBYPASSRLS NOSUPERUSER;
GRANT app_admin TO dotmac_app;
CREATE TABLE public.outbox_events (id int PRIMARY KEY, tenant_id text NOT NULL);
ALTER TABLE public.outbox_events OWNER TO app_admin;
ALTER TABLE public.outbox_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.outbox_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON public.outbox_events
  USING (tenant_id = current_setting('app.current_tenant', true));
GRANT SELECT ON public.outbox_events TO app_user;
INSERT INTO public.outbox_events VALUES (1, 'tenant-a'), (2, 'tenant-b');
CREATE TABLE public.platform_outbox_events (id int PRIMARY KEY);
ALTER TABLE public.platform_outbox_events OWNER TO app_admin;
REVOKE ALL PRIVILEGES ON TABLE public.platform_outbox_events FROM PUBLIC;
REVOKE ALL PRIVILEGES ON TABLE public.platform_outbox_events FROM app_user;
"""


def self_test(image: str) -> dict[str, Any]:
    """Build a synthetic bundle with production flags, then drill it."""

    with tempfile.TemporaryDirectory(prefix="restore-drill-selftest-") as tmp:
        bundle_dir = Path(tmp)
        with ScratchPostgres(image) as source:
            applied = source.psql(FIXTURE_SQL, db=SCRATCH_DB)
            if applied.returncode != 0:
                raise RuntimeError(
                    f"fixture failed: {applied.stderr.decode(errors='replace')[:500]}"
                )
            globals_gz = bundle_dir / "fixture.globals.sql.gz"
            dump_gz = bundle_dir / "fixture.sql.gz"
            for out_path, tool_args in (
                (
                    globals_gz,
                    [
                        "pg_dumpall",
                        "-U",
                        "postgres",
                        "--globals-only",
                        "--no-role-passwords",
                    ],
                ),
                (dump_gz, ["pg_dump", "-U", "postgres", "-d", SCRATCH_DB]),
            ):
                result = run(["docker", "exec", source.name, *tool_args], timeout=300)
                if result.returncode != 0:
                    raise RuntimeError(
                        f"{tool_args[0]} failed: {result.stderr.decode(errors='replace')[:500]}"
                    )
                with gzip.open(out_path, "wb") as handle:
                    handle.write(result.stdout)
        manifest = {
            "schema": "dotmac_sub.recovery_bundle.v1",
            "created_at": "self-test",
            "database": SCRATCH_DB,
            "artifacts": [
                {
                    "role": "globals",
                    "file": globals_gz.name,
                    "sha256": sha256_file(globals_gz),
                    "bytes": globals_gz.stat().st_size,
                },
                {
                    "role": "database_dump",
                    "file": dump_gz.name,
                    "sha256": sha256_file(dump_gz),
                    "bytes": dump_gz.stat().st_size,
                },
            ],
        }
        manifest_path = bundle_dir / "fixture.manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        verdict = drill_bundle(manifest_path, image)
        verdict["mode"] = "self-test"
        return verdict


def dry_run(manifest_path: Path) -> dict[str, Any]:
    """Validate a bundle and print the restore plan without touching docker."""

    manifest, artifacts = load_bundle(manifest_path)
    hash_failures = verify_hashes(manifest, artifacts)
    globals_roles, has_passwords = parse_globals_roles(artifacts["globals"])
    contract, presence_only = expected_role_contract()
    return {
        "verdict": "FAIL" if hash_failures or has_passwords else "DRY_RUN_OK",
        "mode": "dry-run",
        "database": manifest.get("database"),
        "hash_failures": hash_failures,
        "globals_has_password_material": has_passwords,
        "globals_roles": sorted(globals_roles),
        "contract_roles_expected": sorted(contract),
        "presence_only_roles": list(presence_only),
        "plan": [
            "verify sha256 of every artifact against the manifest",
            "start scratch postgres (--network none, tmpfs, generated name)",
            "apply globals (roles, memberships, tablespaces; no passwords)",
            "install login material from the approved secret source",
            "apply database dump with ON_ERROR_STOP=1",
            "assert isolation model (roles/postures, FORCEd RLS, revocation, cross-tenant, ownership)",
            "destroy scratch container",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, help="path to <stem>.manifest.json")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument(
        "--dry-run", action="store_true", help="validate bundle, print plan, no docker"
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="synthetic fixture drill (CI, no prod data)",
    )
    parser.add_argument(
        "--target-container",
        default="",
        help="refused: the drill always generates its own scratch container",
    )
    parser.add_argument("--out", type=Path, help="also write the verdict JSON here")
    args = parser.parse_args()

    if args.target_container:
        verdict = {
            "verdict": "REFUSED",
            "reason": refuse_target(args.target_container, what="container")
            or "user-supplied targets are refused; the drill generates its own scratch container",
        }
        print(json.dumps(verdict, indent=2))
        return EXIT_USAGE
    if args.self_test and args.manifest:
        parser.error("--self-test cannot be combined with --manifest")
    if not args.self_test and not args.manifest:
        parser.error("one of --manifest or --self-test is required")

    try:
        if args.self_test:
            if shutil.which("docker") is None:
                print(
                    json.dumps(
                        {"verdict": "INFRA_FAILURE", "reason": "docker not on PATH"}
                    )
                )
                return EXIT_INFRA
            verdict = self_test(args.image)
        elif args.dry_run:
            verdict = dry_run(args.manifest)
        else:
            if shutil.which("docker") is None:
                print(
                    json.dumps(
                        {"verdict": "INFRA_FAILURE", "reason": "docker not on PATH"}
                    )
                )
                return EXIT_INFRA
            verdict = drill_bundle(args.manifest, args.image)
    except ValueError as exc:
        print(
            json.dumps(
                {"verdict": "FAIL", "stage": "bundle_validation", "reason": str(exc)},
                indent=2,
            )
        )
        return EXIT_ASSERTION
    except RuntimeError as exc:
        print(json.dumps({"verdict": "INFRA_FAILURE", "reason": str(exc)}, indent=2))
        return EXIT_INFRA

    rendered = json.dumps(verdict, indent=2)
    print(rendered)
    if args.out:
        args.out.write_text(rendered + "\n", encoding="utf-8")
    return (
        EXIT_OK if verdict.get("verdict") in {"PASS", "DRY_RUN_OK"} else EXIT_ASSERTION
    )


if __name__ == "__main__":
    raise SystemExit(main())
