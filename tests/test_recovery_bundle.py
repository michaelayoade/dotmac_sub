"""Recovery bundles must restore the isolation model, not just the rows.

Guards the census backup defect (SUB_DEPLOYMENT_EXECUTOR_CENSUS.md): both
backup call sites ran ``pg_dump -d`` only, so no role definitions were ever
captured, and db_backup.sh passed ``--no-owner --no-privileges`` on top.
These tests pin: one shared flag source, a fail-closed globals step, an
atomic manifest-sealed bundle, and a restore drill that refuses
production-like targets.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts/backup/recovery_bundle_lib.sh"
DB_BACKUP = ROOT / "scripts/db_backup.sh"
RCLONE_BACKUP = ROOT / "scripts/backup/backup_dotmac_sub_dbs_to_rclone.sh"


def _drill_module() -> ModuleType:
    # Exercise the supported package boundary, including sibling dependencies.
    from scripts import restore_drill

    return restore_drill


# --- static: dump command lines --------------------------------------------


def test_lib_is_the_single_flag_source_and_refuses_stripping() -> None:
    lib = LIB.read_text(encoding="utf-8")
    assert "pg_dumpall" not in lib or "--globals-only --no-role-passwords" in lib
    assert "--globals-only --no-role-passwords" in lib
    for flag in ("--no-owner", "--no-privileges", "--no-acl"):
        assert flag in lib, f"{flag} must be in the forbidden set"
    # Both producers source the lib instead of restating flags.
    assert "backup/recovery_bundle_lib.sh" in DB_BACKUP.read_text(encoding="utf-8")
    assert "recovery_bundle_lib.sh" in RCLONE_BACKUP.read_text(encoding="utf-8")


@pytest.mark.parametrize("script", [DB_BACKUP, RCLONE_BACKUP])
def test_no_producer_strips_identity_or_skips_globals(script: Path) -> None:
    source = script.read_text(encoding="utf-8")
    command_lines = [
        line
        for line in source.splitlines()
        if "pg_dump" in line and not line.strip().startswith("#")
    ]
    assert command_lines
    for line in command_lines:
        assert "--no-owner" not in line and "--no-privileges" not in line, line
    assert "pg_dumpall" in source, f"{script.name} has no globals step"
    assert "RECOVERY_GLOBALS_FLAGS" in source
    assert "recovery_fail_globals" in source, "globals failure must be fail-closed"
    assert "recovery_write_manifest" in source


def test_rclone_retention_counts_bundles_not_files() -> None:
    source = RCLONE_BACKUP.read_text(encoding="utf-8")
    assert "*.manifest.json" in source
    assert "globals.sql.gz" in source


def test_db_backup_retention_does_not_count_globals_sidecars() -> None:
    source = DB_BACKUP.read_text(encoding="utf-8")
    assert '! -name "${BACKUP_RETENTION_PREFIX}*.globals.sql.gz"' in source


# --- behavioural: bundle production with a fake docker ----------------------


def _bash() -> str:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash required")
    # db_backup.sh already required GNU bash (mapfile) and GNU find
    # (-printf) before bundles existed; BSD hosts cannot run it at all.
    probe = subprocess.run(
        [bash, "-c", "type mapfile >/dev/null 2>&1 && find --version >/dev/null 2>&1"],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("db_backup.sh requires GNU bash (mapfile) and GNU find (-printf)")
    return bash


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _fake_docker(
    fake_bin: Path,
    *,
    globals_fail: bool = False,
    globals_payload: str = "CREATE ROLE app_user;\\n",
) -> None:
    globals_branch = "exit 7" if globals_fail else f"printf '{globals_payload}'; exit 0"
    _write_executable(
        fake_bin / "docker",
        "#!/usr/bin/env bash\nset -eu\n"
        'if [[ "$1" == "inspect" ]]; then exit 0; fi\n'
        'if [[ "$1" == "exec" ]]; then\n'
        '  for arg in "$@"; do\n'
        f'    if [[ "$arg" == "pg_dumpall" ]]; then {globals_branch}; fi\n'
        '    if [[ "$arg" == "--version" ]]; then printf "pg_dump (PostgreSQL) 16.4\\n"; exit 0; fi\n'
        "  done\n"
        "  printf 'database backup payload\\n'; exit 0\n"
        "fi\nexit 1\n",
    )


def _run_db_backup(
    tmp_path: Path, fake_bin: Path
) -> tuple[subprocess.CompletedProcess[str], Path]:
    root_dir = tmp_path / "deploy-root"
    backup_dir = tmp_path / "backups" / "deployments"
    root_dir.mkdir()
    backup_dir.mkdir(parents=True)
    (root_dir / ".env").write_text(
        "DATABASE_URL=postgresql://postgres@db/dotmac_sub\n", encoding="utf-8"
    )
    env = {
        **os.environ,
        "ROOT_DIR": str(root_dir),
        "DB_BACKUP_DIR": str(backup_dir),
        "DB_BACKUP_BASENAME": "dotmac_sub_run_42",
        "DB_BACKUP_RETENTION_PREFIX": "dotmac_sub_run_",
        "DB_BACKUP_RETENTION_COUNT": "5",
    }
    command = f"export PATH={shlex.quote(str(fake_bin))}:/usr/bin:/bin; exec bash {shlex.quote(str(DB_BACKUP))}"
    result = subprocess.run(
        [_bash(), "-c", command],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, backup_dir


def test_db_backup_produces_manifest_sealed_bundle(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_docker(fake_bin)

    result, backup_dir = _run_db_backup(tmp_path, fake_bin)

    assert result.returncode == 0, result.stderr
    dumps = [
        p for p in backup_dir.glob("*.sql.gz") if not p.name.endswith(".globals.sql.gz")
    ]
    globals_files = list(backup_dir.glob("*.globals.sql.gz"))
    manifests = list(backup_dir.glob("*.manifest.json"))
    assert len(dumps) == len(globals_files) == len(manifests) == 1
    assert not list(backup_dir.glob("*.tmp.*")), "temp artifacts must be renamed away"

    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["schema"] == "dotmac_sub.recovery_bundle.v1"
    assert manifest["restore_order"] == ["globals", "database_dump", "verify"]
    by_role = {a["role"]: a for a in manifest["artifacts"]}
    for role, path in (("globals", globals_files[0]), ("database_dump", dumps[0])):
        assert by_role[role]["file"] == path.name
        assert by_role[role]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert by_role[role]["bytes"] == path.stat().st_size
    assert "--no-role-passwords" in by_role["globals"]["command"]


def test_db_backup_globals_failure_is_fail_closed_and_distinct(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_docker(fake_bin, globals_fail=True)

    result, backup_dir = _run_db_backup(tmp_path, fake_bin)

    assert result.returncode == 3, result.stderr
    assert list(backup_dir.glob("*.FAILED")), "a globals failure must leave a marker"
    assert not list(backup_dir.glob("*.sql.gz")), (
        "no dump may survive a globals failure"
    )
    assert not list(backup_dir.glob("*.manifest.json"))


def test_db_backup_refuses_password_verifiers_in_globals(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # \x27 is a single quote, expanded by the fake's printf format string.
    _fake_docker(
        fake_bin,
        globals_payload="CREATE ROLE app_user PASSWORD \\x27SCRAM-SHA-256$4096:abc\\x27;\\n",
    )

    result, backup_dir = _run_db_backup(tmp_path, fake_bin)

    assert result.returncode == 3, result.stderr
    assert not list(backup_dir.glob("*.manifest.json"))


# --- drill unit guards -------------------------------------------------------


def test_drill_refuses_production_like_targets() -> None:
    drill = _drill_module()
    for name in ("dotmac_pg_local", "dotmac_sub_db", "my-prod-db", "PRODUCTION"):
        assert drill.refuse_target(name, what="container") is not None, name
    assert drill.refuse_target("dotmac_sub", what="database") is not None
    assert drill.refuse_target(drill.scratch_container_name(), what="container") is None


def test_drill_parses_globals_roles_and_detects_verifiers(tmp_path: Path) -> None:
    drill = _drill_module()
    globals_path = tmp_path / "g.globals.sql.gz"
    with gzip.open(globals_path, "wt", encoding="utf-8") as handle:
        handle.write(
            "CREATE ROLE app_user;\nALTER ROLE \"app_admin\" WITH LOGIN;\nCREATE ROLE x PASSWORD 'SCRAM-SHA-256$1:a';\n"
        )
    roles, has_passwords = drill.parse_globals_roles(globals_path)
    assert roles == {"app_user", "app_admin", "x"}
    assert has_passwords is True


def test_drill_dry_run_verifies_manifest_hashes(tmp_path: Path) -> None:
    drill = _drill_module()
    globals_path = tmp_path / "s.globals.sql.gz"
    dump_path = tmp_path / "s.sql.gz"
    with gzip.open(globals_path, "wt", encoding="utf-8") as handle:
        handle.write("CREATE ROLE app_user;\nCREATE ROLE app_admin;\n")
    with gzip.open(dump_path, "wt", encoding="utf-8") as handle:
        handle.write("-- dump\n")
    manifest = {
        "schema": "dotmac_sub.recovery_bundle.v1",
        "database": "dotmac_sub",
        "artifacts": [
            {
                "role": "globals",
                "file": globals_path.name,
                "sha256": drill.sha256_file(globals_path),
            },
            {"role": "database_dump", "file": dump_path.name, "sha256": "0" * 64},
        ],
    }
    manifest_path = tmp_path / "s.manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    verdict = drill.dry_run(manifest_path)
    assert verdict["verdict"] == "FAIL"  # hash mismatch must not read as OK
    assert verdict["hash_failures"]

    manifest["artifacts"][1]["sha256"] = drill.sha256_file(dump_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    verdict = drill.dry_run(manifest_path)
    assert verdict["verdict"] == "DRY_RUN_OK"
    assert verdict["globals_roles"] == ["app_admin", "app_user"]
    assert "outbox_dispatcher" in verdict["contract_roles_expected"]


def test_drill_rejects_lone_dump_manifest(tmp_path: Path) -> None:
    drill = _drill_module()
    manifest_path = tmp_path / "s.manifest.json"
    manifest_path.write_text(
        json.dumps({"schema": "dotmac_sub.recovery_bundle.v1", "artifacts": []}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="globals"):
        drill.load_bundle(manifest_path)
