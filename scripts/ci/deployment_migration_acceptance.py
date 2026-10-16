"""Serial, hosted-only acceptance against real immutable images and PostgreSQL.

This development adapter calls deployment migration/control owners directly.
It never invokes the protected deployment wrapper or grants deployment authority.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from scripts import production_deploy_migration as migration
from scripts import production_deploy_probe as probe
from scripts import production_trusted_deploy as trusted
from scripts.production_trusted_rollback_preflight import TrustedPreflightInput
from scripts.release_artifact_contract import (
    GitCommitSha,
    OCIImageDigest,
    WorkflowRunId,
)
from scripts.release_candidate_evidence import read_candidate_evidence

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "ghcr.io/michaelayoade/dotmac_sub"
BASELINE = ROOT / "scripts/ci/fixtures/deployment_migration_predecessor.json"


class AcceptanceRefused(ValueError):
    """Closed acceptance refusal with no private subprocess output."""


@dataclass(frozen=True, slots=True)
class ImageIdentity:
    revision: str
    tree: str
    digest: str
    build_run_id: int

    @property
    def reference(self) -> str:
        return f"{REPOSITORY}@{self.digest}"


def validate_identity(identity: ImageIdentity) -> None:
    if (
        re.fullmatch(r"[0-9a-f]{40}", identity.revision) is None
        or re.fullmatch(r"[0-9a-f]{40}", identity.tree) is None
        or re.fullmatch(r"sha256:[0-9a-f]{64}", identity.digest) is None
        or type(identity.build_run_id) is not int
        or identity.build_run_id <= 0
    ):
        raise AcceptanceRefused("image_identity_unproved")


def require_hosted_dispatch(environment: dict[str, str]) -> None:
    if (
        environment.get("GITHUB_ACTIONS") != "true"
        or environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or environment.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or environment.get("GITHUB_WORKFLOW") != "Build release candidate once"
    ):
        raise AcceptanceRefused("hosted_dispatch_required")


def command(arguments: list[str], *, timeout: int = 120) -> str:
    result = subprocess.run(
        arguments, capture_output=True, text=True, check=False, timeout=timeout
    )
    if result.returncode:
        # Never echo Docker logs, environment, SQL, or authorization fixtures.
        raise AcceptanceRefused("acceptance_command_failed")
    return result.stdout.strip()


def expect_refusal(action: Callable[[], object], *, code: str | None = None) -> str:
    try:
        action()
    except (migration.MigrationRefused, probe.ProbeRefused) as exc:
        if code is not None and str(exc) != code:
            raise AcceptanceRefused("unexpected_refusal") from None
        return str(exc)
    raise AcceptanceRefused("refusal_canary_accepted")


@dataclass(frozen=True, slots=True)
class ScenarioOutcome:
    scenario: str
    status: str
    fault: str
    phases: tuple[str, ...] = ()
    invocation_id: str | None = None
    container_id: str | None = None
    heads_before: tuple[str, ...] = ()
    heads_after: tuple[str, ...] = ()
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class HeldDatabaseLock:
    process: subprocess.Popen[bytes]
    database: str
    application_name: str
    backend_pid: int
    backend_started_at: str


@dataclass(frozen=True, slots=True)
class MigrationScenarioSpec:
    deploy_directory: Path
    binding: probe.CapturedNetworkBinding
    run_id: int
    authorization_run_id: int
    heads_before: tuple[str, ...]
    candidate_heads: tuple[str, ...]
    budget_seconds: int

    def write(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "deploy_directory": str(self.deploy_directory),
                    "binding": self.binding.public_json(),
                    "run_id": self.run_id,
                    "authorization_run_id": self.authorization_run_id,
                    "heads_before": self.heads_before,
                    "candidate_heads": self.candidate_heads,
                    "budget_seconds": self.budget_seconds,
                }
            )
        )

    @classmethod
    def read(cls, path: Path) -> MigrationScenarioSpec:
        document = json.loads(path.read_text())
        if type(document) is not dict or set(document) != {
            "deploy_directory",
            "binding",
            "run_id",
            "authorization_run_id",
            "heads_before",
            "candidate_heads",
            "budget_seconds",
        }:
            raise AcceptanceRefused("scenario_spec_unproved")
        if (
            type(document["deploy_directory"]) is not str
            or not Path(document["deploy_directory"]).is_absolute()
            or any(
                type(document[key]) is not int or document[key] <= 0
                for key in ("run_id", "authorization_run_id", "budget_seconds")
            )
            or any(
                type(document[key]) is not list
                or any(type(head) is not str for head in document[key])
                for key in ("heads_before", "candidate_heads")
            )
        ):
            raise AcceptanceRefused("scenario_spec_unproved")
        return cls(
            Path(document["deploy_directory"]),
            probe._binding_from_json(json.dumps(document["binding"])),
            document["run_id"],
            document["authorization_run_id"],
            tuple(document["heads_before"]),
            tuple(document["candidate_heads"]),
            document["budget_seconds"],
        )


class HostedAcceptance:
    """Own only the explicitly recorded disposable fixture resources."""

    def __init__(
        self,
        *,
        directory: Path,
        candidate: ImageIdentity,
        predecessor: ImageIdentity,
        predecessor_source: Path,
        candidate_evidence: Path,
        predecessor_evidence: Path,
    ) -> None:
        self.directory = directory
        self.candidate = candidate
        self.predecessor = predecessor
        self.predecessor_source = predecessor_source
        self.candidate_evidence = candidate_evidence
        self.predecessor_evidence = predecessor_evidence
        self.name = f"migration_ci_{uuid.uuid4().hex[:12]}"
        self.containers: list[str] = []
        self.owned_container_ids: set[str] = set()
        self.lock_processes: list[subprocess.Popen[bytes]] = []
        self.volumes: list[str] = []
        self.network: str | None = None
        self.database: str = ""
        self.broker: str = ""
        self.outcomes: list[ScenarioOutcome] = []
        self.counter = 0
        self.cluster_password = uuid.uuid4().hex
        self.migration_password = uuid.uuid4().hex
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=False)

    def docker(self, *arguments: str, timeout: int = 120) -> str:
        return command(["docker", *arguments], timeout=timeout)

    def create(self, *arguments: str) -> str:
        identifier = self.docker("create", *arguments)
        if re.fullmatch(r"[0-9a-f]{64}", identifier) is None:
            raise AcceptanceRefused("fixture_container_identity_unproved")
        self.containers.append(identifier)
        self.owned_container_ids.add(identifier)
        return identifier

    def sql(self, statement: str, *, database: str = "postgres") -> str:
        return self.docker(
            "exec",
            self.database,
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            database,
            "-Atc",
            statement,
        )

    def hold_database_lock(self) -> HeldDatabaseLock:
        database = f"migration_ci_{self.counter}"
        application_name = f"migration_ci_lock_{self.counter}"
        process = subprocess.Popen(
            [
                "docker",
                "exec",
                self.database,
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "postgres",
                "-d",
                database,
                "-c",
                f"SET application_name='{application_name}'; BEGIN; "
                "LOCK TABLE public.topup_intents IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(600); COMMIT;",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.lock_processes.append(process)
        for _attempt in range(100):
            observation = self.sql(
                "SELECT a.pid::text || '|' || a.backend_start::text "
                "FROM pg_stat_activity a JOIN pg_locks l ON l.pid=a.pid "
                f"WHERE a.datname='{database}' AND a.application_name='{application_name}' "
                "AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database()) "
                "AND l.relation='public.topup_intents'::regclass AND l.mode='AccessExclusiveLock' AND l.granted",
                database=database,
            )
            if observation:
                fields = observation.split("|")
                if (
                    len(fields) != 2
                    or not fields[0].isdigit()
                    or re.fullmatch(r"[0-9:.+ -]+", fields[1]) is None
                ):
                    raise AcceptanceRefused("fixture_lock_backend_ambiguous")
                return HeldDatabaseLock(
                    process, database, application_name, int(fields[0]), fields[1]
                )
            time.sleep(0.1)
        # This process is fixture-owned, but its unknown backend must never be
        # terminated by a broad database/session predicate.
        process.terminate()
        process.wait(timeout=10)
        raise AcceptanceRefused("real_lock_not_acquired")

    def release_database_lock(self, held: HeldDatabaseLock) -> None:
        released = self.sql(
            "SELECT pg_terminate_backend(a.pid) FROM pg_stat_activity a "
            f"WHERE a.pid={held.backend_pid} AND a.datname='{held.database}' "
            f"AND a.application_name='{held.application_name}' "
            f"AND a.backend_start::text='{held.backend_started_at}' "
            "AND EXISTS (SELECT 1 FROM pg_locks l WHERE l.pid=a.pid "
            "AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database()) "
            "AND l.relation='public.topup_intents'::regclass AND l.mode='AccessExclusiveLock' AND l.granted)",
            database=held.database,
        )
        held.process.wait(timeout=15)
        if released != "t":
            raise AcceptanceRefused("exact_fixture_lock_release_unproved")

    def image_command(
        self,
        image: ImageIdentity,
        database: str,
        *arguments: str,
        bootstrap: bool = False,
    ) -> str:
        role = "postgres" if bootstrap else "dotmac_app"
        password = self.cluster_password if bootstrap else self.migration_password
        identifier = self.create(
            "--network",
            self.network or "",
            "--entrypoint",
            "/opt/venv/bin/python",
            "-e",
            f"DATABASE_URL=postgresql+psycopg://{role}:{password}@postgres-local/{database}",
            "-e",
            f"MIGRATION_DATABASE_URL=postgresql://{role}:{password}@postgres-local/{database}",
            "-e",
            f"BOOTSTRAP_DATABASE_URL=postgresql://{role}:{password}@postgres-local/{database}",
            image.reference,
            *arguments,
        )
        self.docker("start", identifier)
        code = self.docker("wait", identifier, timeout=600)
        output = self.docker("logs", identifier)
        self.docker("rm", identifier)
        self.containers.remove(identifier)
        if code != "0":
            raise AcceptanceRefused("real_image_owner_failed")
        return output

    def verify_image(
        self, identity: ImageIdentity, source: Path, evidence_path: Path
    ) -> None:
        validate_identity(identity)
        evidence = read_candidate_evidence(evidence_path)
        if (
            evidence.source_revision.value != identity.revision
            or evidence.source_tree.value != identity.tree
            or evidence.image_digest.value != identity.digest
            or evidence.build_run_id.value != identity.build_run_id
            or command(["git", "-C", str(source), "rev-parse", "HEAD"])
            != identity.revision
            or command(["git", "-C", str(source), "rev-parse", "HEAD^{tree}"])
            != identity.tree
        ):
            raise AcceptanceRefused("source_artifact_identity_mismatch")
        run = json.loads(
            command(
                [
                    "gh",
                    "api",
                    f"repos/michaelayoade/dotmac_sub/actions/runs/{identity.build_run_id}",
                ]
            )
        )
        if (
            run.get("head_sha") != identity.revision
            or run.get("event") != "workflow_dispatch"
            or run.get("path") != ".github/workflows/release-candidate.yml"
            # The current build is running while its acceptance job executes.
            or (identity != self.candidate and run.get("conclusion") != "success")
        ):
            raise AcceptanceRefused("build_provenance_unproved")
        self.docker("pull", identity.reference, timeout=600)
        inspected = json.loads(self.docker("image", "inspect", identity.reference))[0]
        labels = inspected["Config"]["Labels"]
        if (
            identity.reference not in inspected["RepoDigests"]
            or labels.get("org.opencontainers.image.revision") != identity.revision
            or labels.get("io.dotmac.release.source-tree") != identity.tree
            or labels.get("io.dotmac.release.build-run") != str(identity.build_run_id)
        ):
            raise AcceptanceRefused("image_provenance_unproved")
        identifier = self.create(identity.reference)
        destination = self.directory / f"embedded-{identity.revision}"
        destination.mkdir()
        # Compare every tracked Python migration and both privileged owners,
        # including external module migration providers under app/.
        paths = command(
            [
                "git",
                "-C",
                str(source),
                "ls-files",
                "alembic",
                "app",
                "scripts",
            ]
        ).splitlines()
        selected = [
            path
            for path in paths
            if path.endswith(".py")
            and (
                path.startswith("alembic/")
                or "migration" in path
                or path
                in {
                    "scripts/bootstrap_commercial_module_prereqs.py",
                    "scripts/bootstrap_outbox_dispatcher_roles.py",
                }
            )
        ]
        for top in ("alembic", "app", "scripts"):
            self.docker("cp", f"{identifier}:/app/{top}", str(destination / top))
        for path in selected:
            if (source / path).read_bytes() != (destination / path).read_bytes():
                raise AcceptanceRefused("embedded_migration_source_mismatch")
        self.docker("rm", identifier)
        self.containers.remove(identifier)

    def prepare(self) -> None:
        self.verify_image(
            self.predecessor, self.predecessor_source, self.predecessor_evidence
        )
        self.verify_image(self.candidate, ROOT, self.candidate_evidence)
        self.network = self.docker("network", "create", "--internal", self.name)
        volume = self.docker("volume", "create", self.name)
        self.volumes.append(volume)
        self.database = self.create(
            "--network",
            self.network,
            "--network-alias",
            "postgres-local",
            "--mount",
            f"type=volume,source={volume},target=/var/lib/postgresql/data",
            "-e",
            f"POSTGRES_PASSWORD={self.cluster_password}",
            "postgis/postgis:16-3.4",
        )
        self.broker = self.create(
            "--network",
            self.network,
            "--network-alias",
            "redis-local",
            "redis:7-alpine",
        )
        self.docker("start", self.database, self.broker)
        for _attempt in range(60):
            ready = subprocess.run(
                ["docker", "exec", self.database, "pg_isready", "-U", "postgres"],
                capture_output=True,
                check=False,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise AcceptanceRefused("postgres_not_ready")
        self.sql("CREATE DATABASE migration_ci_baseline")
        for owner in (
            "scripts/bootstrap_commercial_module_prereqs.py",
            "scripts/bootstrap_outbox_dispatcher_roles.py",
        ):
            self.image_command(
                self.predecessor,
                "migration_ci_baseline",
                owner,
                "--repair",
                bootstrap=True,
            )
        # Existing bootstrap owns role posture, membership and schema grants.
        # This disposable cluster ceremony supplies private authentication
        # and extensions explicitly required by the real migration chain.
        self.sql(f"ALTER ROLE dotmac_app PASSWORD '{self.migration_password}'")
        for extension in ("postgis", "postgis_topology", "pg_trgm", "btree_gist"):
            self.sql(
                f"CREATE EXTENSION IF NOT EXISTS {extension}",
                database="migration_ci_baseline",
            )
        if (
            self.sql(
                "SELECT rolcanlogin AND NOT rolsuper AND NOT rolbypassrls AND NOT rolcreatedb "
                "AND NOT rolcreaterole AND pg_has_role('dotmac_app','app_admin','MEMBER') "
                "FROM pg_roles WHERE rolname='dotmac_app'"
            )
            != "t"
        ):
            raise AcceptanceRefused("canonical_migration_role_posture_unproved")
        if (
            self.sql(
                "SELECT count(*)=3 AND bool_and(rolcanlogin AND NOT rolsuper AND NOT rolcreatedb "
                "AND NOT rolcreaterole AND (rolbypassrls=(rolname='app_admin'))) "
                "FROM pg_roles WHERE rolname IN ('dotmac_app','app_admin','app_user')"
            )
            != "t"
        ):
            raise AcceptanceRefused("canonical_runtime_role_posture_unproved")
        for owner in (
            "scripts/bootstrap_commercial_module_prereqs.py",
            "scripts/bootstrap_outbox_dispatcher_roles.py",
        ):
            self.image_command(
                self.predecessor, "migration_ci_baseline", owner, "--verify-only"
            )
        self.image_command(
            self.predecessor,
            "migration_ci_baseline",
            "-m",
            "alembic",
            "upgrade",
            "heads",
        )
        # No fake Alembic version, stamp, current-source downgrade, or ORM setup.
        if (
            self.sql(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' "
                "AND table_name='topup_intents' AND column_name IN ('module_intent_id','module_intent_tenant_id')",
                database="migration_ci_baseline",
            )
            != "0"
        ):
            raise AcceptanceRefused("baseline_is_not_real_pre691_schema")

    def fixture(self) -> tuple[Path, probe.CapturedNetworkBinding]:
        previous_ids = set(self.containers)
        try:
            return self._create_fixture()
        except (
            AcceptanceRefused,
            probe.ProbeRefused,
            OSError,
            ValueError,
            subprocess.SubprocessError,
        ):
            for identifier in tuple(self.containers):
                if identifier not in previous_ids:
                    result = subprocess.run(
                        ["docker", "rm", "-f", identifier],
                        capture_output=True,
                        check=False,
                    )
                    if result.returncode == 0:
                        self.containers.remove(identifier)
            raise

    def _create_fixture(self) -> tuple[Path, probe.CapturedNetworkBinding]:
        self.counter += 1
        database = f"migration_ci_{self.counter}"
        self.sql(f"CREATE DATABASE {database} TEMPLATE migration_ci_baseline")
        directory = self.directory / f"scenario-{self.counter}"
        directory.mkdir(mode=0o700)
        database_url = f"postgresql+psycopg://dotmac_app:{self.migration_password}@postgres-local/{database}"
        environment = {
            "COMPOSE_PROJECT_NAME": self.name,
            "APP_IMAGE": self.candidate.reference,
            "DATABASE_URL": database_url,
            "CELERY_BROKER_URL": "redis://redis-local:6379/0",
            "CELERY_RESULT_BACKEND": "redis://redis-local:6379/0",
            "REDIS_URL": "redis://redis-local:6379/0",
        }
        (directory / ".env").write_text(
            "".join(f"{key}={value}\n" for key, value in environment.items())
        )
        os.chmod(directory / ".env", 0o600)
        compose = (
            "--project-directory",
            str(directory),
            "--env-file",
            str(directory / ".env"),
            "-f",
            str(ROOT / "docker-compose.yml"),
        )
        services = probe._compose_services(
            compose,
            image_reference=self.candidate.reference,
            revision=self.candidate.revision,
        )
        app_environment = probe._compose_environment(services["app"])
        env_args = [
            item
            for key, value in app_environment.items()
            for item in ("-e", f"{key}={value}")
        ]
        hosts = [
            item
            for binding in probe._compose_extra_hosts(services["app"])
            for item in ("--add-host", f"{binding.hostname}:{binding.address}")
        ]
        app = self.create(
            "--name",
            "dotmac_sub_app",
            "--network",
            self.network or "",
            "--label",
            f"com.docker.compose.project={self.name}",
            "--label",
            "com.docker.compose.service=app",
            "--entrypoint",
            "/opt/venv/bin/python",
            *hosts,
            *env_args,
            self.predecessor.reference,
            "-c",
            "import time; time.sleep(3600)",
        )
        self.docker("start", app)
        worker_env = probe._compose_environment(services["celery-worker"])
        worker_args = [
            item
            for key, value in worker_env.items()
            for item in ("-e", f"{key}={value}")
        ]
        worker = self.create(
            "--network",
            self.network or "",
            "--label",
            f"com.docker.compose.project={self.name}",
            "--label",
            "com.docker.compose.service=celery-worker",
            "--entrypoint",
            "/opt/venv/bin/python",
            *hosts,
            *worker_args,
            self.predecessor.reference,
            "-c",
            "import time; time.sleep(3600)",
        )
        self.docker("start", worker)
        inactive = tuple(
            sorted(
                name
                for name, service in services.items()
                if name not in {"app", "celery-worker"}
                and type(service) is dict
                and str(service.get("image", "")).startswith(REPOSITORY)
            )
        )
        binding = probe._capture(
            app_container=app,
            writer_specs=(f"celery-worker={worker}",),
            candidate_reference=self.candidate.reference,
            candidate_revision=self.candidate.revision,
            inactive_services=inactive,
            compose_arguments=compose,
        )
        self.docker("stop", app, worker)
        return directory, binding

    def finish_fixture(self, binding: probe.CapturedNetworkBinding) -> None:
        self.docker("rm", "-f", binding.app_container_id)
        self.containers.remove(binding.app_container_id)
        for writer in binding.writers:
            self.docker("rm", "-f", writer.container_id)
            self.containers.remove(writer.container_id)

    def invoke(
        self,
        directory: Path,
        binding: probe.CapturedNetworkBinding,
        *,
        fault: str,
        budget: int = 120,
    ) -> tuple[int, migration.MigrationInvocation]:
        before = migration._observe_heads(binding, image="candidate", current=True)
        heads = migration._observe_heads(binding, image="candidate", current=False)
        spec = directory / "invocation.json"
        MigrationScenarioSpec(
            directory, binding, self.counter + 1000, 1, before, heads, budget
        ).write(spec)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.ci.deployment_migration_fault_child",
                "--spec",
                str(spec),
                "--fault",
                fault,
            ],
            cwd=ROOT,
            capture_output=True,
            check=False,
            timeout=300,
        )
        record, _digest = migration._load_journal(migration._journal_path(directory))
        if record is None:
            raise AcceptanceRefused("real_invocation_journal_missing")
        return result.returncode, record

    def reconcile(self, directory: Path) -> migration.MigrationReconciliationOutcome:
        record, digest = migration._load_journal(migration._journal_path(directory))
        if record is None or digest is None:
            raise AcceptanceRefused("journal_missing")
        return migration.reconcile_pending(
            migration.MigrationReconciliationRequest(
                directory,
                record.identity.invocation_id,
                digest,
            )
        )

    def remember_runner(self, record: migration.MigrationInvocation) -> None:
        # A canonical typed journal ID is an observation target even after
        # ordinary removal. Only the exact live inspector below authorizes
        # enrollment in the separate force-cleanup list.
        if record.identity.container_id is not None:
            self.owned_container_ids.add(record.identity.container_id)
        # CREATE acknowledgement loss leaves ID null; resolve only by the
        # exact journal-owned name and verify using the production reconciler.
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.Id}}",
                record.identity.container_name,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            identifier = result.stdout.strip()
            if re.fullmatch(r"[0-9a-f]{64}", identifier) is None:
                raise AcceptanceRefused("runner_cleanup_identity_unproved")
            if (
                record.identity.container_id is not None
                and identifier != record.identity.container_id
            ):
                raise AcceptanceRefused("runner_cleanup_identity_changed")
            binding = probe._binding_from_json(json.dumps(record.network_binding))
            identity = replace(record.identity, container_id=identifier)
            observed = migration._inspect(identifier)
            state = observed.get("State")
            status = state.get("Status") if type(state) is dict else None
            if status not in {"created", "running", "exited"}:
                raise AcceptanceRefused("runner_cleanup_state_unproved")
            migration._inspect_exact(
                identity,
                binding,
                expected_status=status,
                runtime_config=probe._verify(binding, allow_stopped_app=True),
            )
            if identifier not in self.containers:
                self.containers.append(identifier)
            self.owned_container_ids.add(identifier)

    def scenario(
        self, fault: str, *, hold_lock: bool = False, budget: int = 120
    ) -> None:
        directory, binding = self.fixture()
        lock: HeldDatabaseLock | None = None
        try:
            if hold_lock:
                lock = self.hold_database_lock()
            returncode, record = self.invoke(
                directory, binding, fault=fault, budget=budget
            )
            original_record = record
            if fault.startswith("crash-") and returncode != 86:
                raise AcceptanceRefused("requested_durable_phase_crash_not_reached")
            if fault in {"ack-create", "ack-remove"} and returncode != 3:
                raise AcceptanceRefused("requested_ack_loss_not_reached")
            if fault.startswith("ack-"):
                fault_event = json.loads((directory / "fault-event.json").read_text())
                if fault_event != {
                    "fault": fault,
                    "operation": {
                        "ack-create": "create",
                        "ack-start": "start",
                        "ack-remove": "rm",
                    }[fault],
                    "invocation_id": record.identity.invocation_id,
                }:
                    raise AcceptanceRefused("requested_ack_loss_not_proved")
            phases = [
                json.loads(line)["phase"]
                for line in (directory / "phase-events.jsonl").read_text().splitlines()
            ]
            if hold_lock and budget == 1:
                if record.phase is not migration.MigrationPhase.INDETERMINATE:
                    raise AcceptanceRefused("timeout_did_not_retain_runner")
                if record.identity.container_id is None:
                    raise AcceptanceRefused("timeout_runner_missing")
                observed = json.loads(
                    self.docker("inspect", record.identity.container_id)
                )[0]
                if observed["State"]["Status"] != "running":
                    raise AcceptanceRefused("timeout_runner_not_retained_running")
            if lock is not None:
                self.release_database_lock(lock)
                lock = None
            self.remember_runner(record)
            if fault != "none" or hold_lock:
                terminal_before_reconcile: (
                    migration.MigrationTerminalEvidence | None
                ) = None
                if record.identity.container_id is not None:
                    inspection = subprocess.run(
                        ["docker", "inspect", record.identity.container_id],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    inspected = (
                        json.loads(inspection.stdout)[0]
                        if inspection.returncode == 0
                        else None
                    )
                    if (
                        inspected is not None
                        and inspected["State"]["Status"] == "running"
                    ):
                        self.docker("wait", record.identity.container_id, timeout=90)
                    if inspected is not None:
                        inspected = migration._inspect(record.identity.container_id)
                        if inspected["State"]["Status"] == "exited":
                            exact = migration._inspect_exact(
                                record.identity,
                                binding,
                                expected_status="exited",
                                runtime_config=probe._verify(
                                    binding, allow_stopped_app=True
                                ),
                            )
                            terminal_before_reconcile = migration._terminal(
                                record.identity, binding, exact
                            )
                reconciled = self.reconcile(directory)
                phases.append(reconciled.phase.value)
                record, digest = migration._load_journal(
                    migration._journal_path(directory)
                )
                if record is None or digest is None:
                    raise AcceptanceRefused("reconciled_journal_missing")
                expect_refusal(
                    lambda: migration.reconcile_pending(
                        migration.MigrationReconciliationRequest(
                            directory, str(uuid.uuid4()), digest
                        )
                    ),
                    code="migration_reconciliation_stale",
                )
                expect_refusal(
                    lambda: migration.reconcile_pending(
                        migration.MigrationReconciliationRequest(
                            directory, record.identity.invocation_id, "0" * 64
                        )
                    ),
                    code="migration_reconciliation_stale",
                )
            after = migration._observe_heads(binding, image="candidate", current=True)
            before_start = {
                "crash-create-requested": migration.MigrationPhase.CREATE_REQUESTED,
                "ack-create": migration.MigrationPhase.CREATED,
                "crash-created": migration.MigrationPhase.CREATED,
                "crash-start-requested": migration.MigrationPhase.START_REQUESTED,
            }
            retained_exited = {
                "crash-running",
                "crash-exited",
                "crash-remove-requested",
            }
            if fault in before_start:
                identity = record.identity
                original_identity = original_record.identity
                if (
                    replace(identity, container_id=original_identity.container_id)
                    != original_identity
                    or (
                        original_identity.container_id is not None
                        and identity.container_id != original_identity.container_id
                    )
                    or (
                        fault == "crash-create-requested"
                        and identity.container_id is not None
                    )
                    or (fault == "ack-create" and identity.container_id is None)
                    or record.heads_before != original_record.heads_before
                    or record.candidate_heads != original_record.candidate_heads
                ):
                    raise AcceptanceRefused(
                        "prestart_reconciliation_replaced_invocation"
                    )
                if (
                    reconciled.disposition
                    is not migration.MigrationDisposition.UNRESOLVED
                    or record.phase is not before_start[fault]
                    or after != record.heads_before
                    or record.terminal is not None
                ):
                    raise AcceptanceRefused(
                        "prestart_reconciliation_advanced_runner_or_schema"
                    )
                if fault == "crash-create-requested":
                    # No ID exists at this durable pre-CREATE boundary. This
                    # canary observes exact name/UUID inventory only; it never
                    # derives completion or schema safety from absence.
                    by_name = self.docker(
                        "container",
                        "ls",
                        "-a",
                        "--no-trunc",
                        "--filter",
                        f"name=^/{record.identity.container_name}$",
                        "--format",
                        "{{.ID}}",
                    )
                    by_invocation = self.docker(
                        "container",
                        "ls",
                        "-a",
                        "--no-trunc",
                        "--filter",
                        f"label=com.dotmac.production-deploy.invocation-id={record.identity.invocation_id}",
                        "--format",
                        "{{.ID}}",
                    )
                    if by_name or by_invocation:
                        raise AcceptanceRefused("precreate_crash_created_runner")
                else:
                    exact = migration._inspect_exact(
                        record.identity,
                        binding,
                        expected_status="created",
                        runtime_config=probe._verify(binding, allow_stopped_app=True),
                    )
                    if exact["RestartCount"] != 0 or not exact["State"][
                        "StartedAt"
                    ].startswith("0001-"):
                        raise AcceptanceRefused(
                            "prestart_runner_was_started_or_retried"
                        )
            elif hold_lock and budget != 1:
                if record.phase is not migration.MigrationPhase.FORWARD_REPAIR_REQUIRED:
                    raise AcceptanceRefused("pre691_lock_failure_wrong_disposition")
            elif (
                hold_lock
                or fault in retained_exited
                or fault
                in {
                    "none",
                    "ack-start",
                    "ack-remove",
                    "crash-removed",
                    "crash-complete",
                }
            ):
                if record.phase is not migration.MigrationPhase.COMPLETE:
                    raise AcceptanceRefused("upgrade_not_complete")
                if after != record.candidate_heads:
                    raise AcceptanceRefused("effective_heads_mismatch")
                if fault == "none" and not hold_lock and returncode != 0:
                    raise AcceptanceRefused("upgrade_returncode_nonzero")
                if (
                    fault == "none"
                    and not hold_lock
                    and not migration._exact_container_absence_proven(record.identity)
                ):
                    raise AcceptanceRefused("successful_runner_cleanup_unproved")
                if fault in retained_exited:
                    exact = migration._inspect_exact(
                        record.identity,
                        binding,
                        expected_status="exited",
                        runtime_config=probe._verify(binding, allow_stopped_app=True),
                    )
                    if (
                        terminal_before_reconcile is None
                        or record.terminal != terminal_before_reconcile
                        or migration._terminal(record.identity, binding, exact)
                        != terminal_before_reconcile
                    ):
                        raise AcceptanceRefused(
                            "retained_exited_reconciliation_changed_runner"
                        )
            self.outcomes.append(
                ScenarioOutcome(
                    scenario=("timeout-retained" if budget == 1 else "lock-nonzero")
                    if hold_lock
                    else fault,
                    status="passed",
                    fault=fault,
                    phases=tuple(phases),
                    invocation_id=record.identity.invocation_id,
                    container_id=record.identity.container_id,
                    heads_before=record.heads_before,
                    heads_after=after,
                )
            )
        finally:
            if lock is not None:
                self.release_database_lock(lock)
            if "record" not in locals():
                record, _digest = migration._load_journal(
                    migration._journal_path(directory)
                )
            if record is not None:
                self.remember_runner(record)
            self.finish_fixture(binding)
            if record is not None:
                for identifier in tuple(self.containers):
                    if identifier not in {self.database, self.broker}:
                        self.docker("rm", "-f", identifier)
                        self.containers.remove(identifier)

    def cleanup(self) -> bool:
        for identifier in reversed(self.containers):
            subprocess.run(
                ["docker", "rm", "-f", identifier], capture_output=True, check=False
            )
        for volume in self.volumes:
            subprocess.run(
                ["docker", "volume", "rm", volume], capture_output=True, check=False
            )
        for process in self.lock_processes:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
        if self.network is not None:
            subprocess.run(
                ["docker", "network", "rm", self.network],
                capture_output=True,
                check=False,
            )
        try:
            remaining_containers = set(
                self.docker(
                    "ps", "-a", "--no-trunc", "--format", "{{.ID}}"
                ).splitlines()
            )
            remaining_volumes = set(
                self.docker("volume", "ls", "--format", "{{.Name}}").splitlines()
            )
            remaining_networks = set(
                self.docker(
                    "network", "ls", "--no-trunc", "--format", "{{.ID}}"
                ).splitlines()
            )
            return not (
                remaining_containers.intersection(self.owned_container_ids)
                or remaining_volumes.intersection(self.volumes)
                or self.network in remaining_networks
            )
        except (AcceptanceRefused, OSError, subprocess.SubprocessError):
            return False

    def maintenance_canaries(self, *, known_predecessor: bool = False) -> None:
        directory, binding = self.fixture()
        context_fd: int | None = None
        lock_fd: int | None = None
        database_lock: HeldDatabaseLock | None = None
        try:
            if known_predecessor:
                database_lock = self.hold_database_lock()
                returncode, record = self.invoke(
                    directory, binding, fault="crash-exited"
                )
                self.remember_runner(record)
                self.release_database_lock(database_lock)
                database_lock = None
                if (
                    returncode != 86
                    or record.phase is not migration.MigrationPhase.EXITED
                ):
                    raise AcceptanceRefused("retained_real_lock_failure_unproved")
                outcome = self.reconcile(directory)
                record, _digest = migration._load_journal(
                    migration._journal_path(directory)
                )
                if (
                    outcome.disposition
                    is not migration.MigrationDisposition.FORWARD_REPAIR_REQUIRED
                    or record is None
                    or record.phase
                    is not migration.MigrationPhase.FORWARD_REPAIR_REQUIRED
                    or record.terminal is None
                    or record.terminal.exit_code == 0
                    or record.terminal.oom_killed
                    or record.terminal.restart_count != 0
                    or migration._observe_heads(
                        binding, image="candidate", current=True
                    )
                    != record.heads_before
                ):
                    raise AcceptanceRefused(
                        "known_predecessor_failed_invocation_unproved"
                    )
            else:
                _returncode, record = self.invoke(directory, binding, fault="none")
            if (
                not known_predecessor
                and record.phase is not migration.MigrationPhase.COMPLETE
            ):
                raise AcceptanceRefused("maintenance_installed_fixture_failed")
            # LOCAL parser/control fixture only. It is destroyed with this
            # private directory and is never an upload or production approval.
            candidate = json.loads(self.candidate_evidence.read_text())
            authorization = {
                key: candidate[key]
                for key in (
                    "schema_version",
                    "source_revision",
                    "source_tree",
                    "image_digest",
                    "product_manifest_digest",
                    "build_run_id",
                    "evidence_run_id",
                )
            }
            authorization.update(
                {
                    "kind": "dotmac.production_authorization",
                    "staging_deployment_id": 1,
                    "authorization_run_id": 1,
                    "authorization_main_revision": self.candidate.revision,
                    "release_revision": self.candidate.revision,
                    "release_tree": self.candidate.tree,
                }
            )
            path = directory / "LOCAL-parser-control-fixture.json"
            path.write_text(json.dumps(authorization))
            os.chmod(path, 0o600)
            preflight = TrustedPreflightInput(
                production_authorization_path=path,
                authorization_run_id=WorkflowRunId(1),
                authorization_main_revision=GitCommitSha(self.candidate.revision),
                target_revision=GitCommitSha(self.candidate.revision),
                target_image_digest=OCIImageDigest(self.candidate.digest),
                deploy_directory=directory,
                rollback_authorization_path=None,
                bootstrap_authorization_path=None,
                control_repository=ROOT,
            )
            os.environ["DEPLOY_RUN_ID"] = str(self.counter + 2000)
            lock_fd = trusted._acquire_canonical_lock()
            context, transition = trusted._maintenance_repair_context(
                trusted.TrustedDeploymentInput(
                    preflight=preflight,
                    staged_repository=ROOT,
                    options=trusted.DeploymentOptions(),
                )
            )
            if transition.value != "same":
                raise AcceptanceRefused("same_transition_unproved")
            admission = migration.validate_maintenance_admission(context, binding)
            if known_predecessor and (
                admission.basis
                is not migration.MaintenanceForwardRepairBasis.UNCHANGED_KNOWN_PREDECESSOR
                or admission.start_heads != record.heads_before
            ):
                raise AcceptanceRefused("known_predecessor_resume_basis_unproved")
            if known_predecessor:
                email_floor = probe._validate_payment_email_floor_output(
                    "payment-email-installed",
                    probe._probe(
                        binding,
                        image="candidate",
                        module="scripts.verify_payment_email_rollback",
                        arguments=("inspect",),
                        operation="payment-email-installed",
                    ),
                )
                payments_floor = probe._validate_payments_floor_output(
                    "payments-supported",
                    probe._probe(
                        binding,
                        image="candidate",
                        module="scripts.verify_payments_intent_rollback",
                        arguments=("inspect",),
                        operation="payments-supported",
                    ),
                )
                if (
                    email_floor["floor"] != "installed"
                    or payments_floor["floor"] != "predecessor"
                ):
                    raise AcceptanceRefused("known_predecessor_real_floors_unproved")
            context_fd = trusted._maintenance_context_fd(context)
            parsed = migration.read_maintenance_context(context_fd)
            if parsed != context or context_fd != 7 or lock_fd != 8:
                raise AcceptanceRefused("canonical_fd_contract_unproved")
            child_result = trusted._wait_for_child(
                [
                    sys.executable,
                    "-m",
                    "scripts.production_deploy_migration",
                    "maintenance-context",
                    "--fd",
                    "7",
                ],
                environment=dict(os.environ),
                cwd=ROOT,
                extra_pass_fds=(7,),
            )
            if child_result != 0:
                raise AcceptanceRefused("inherited_fd_context_unproved")
            expect_refusal(
                lambda: migration.validate_maintenance_admission(
                    replace(context, failed_invocation_id=str(uuid.uuid4())),
                    binding,
                ),
                code="maintenance_journal_unproved",
            )
            expect_refusal(
                lambda: migration.validate_maintenance_admission(
                    replace(context, failed_journal_sha256="0" * 64),
                    binding,
                ),
                code="maintenance_journal_unproved",
            )
            baseline_doc = json.loads(self.predecessor_evidence.read_text())
            backward_doc = dict(authorization)
            for key in (
                "source_revision",
                "source_tree",
                "image_digest",
                "product_manifest_digest",
                "build_run_id",
                "evidence_run_id",
            ):
                backward_doc[key] = baseline_doc[key]
            backward_doc["release_revision"] = self.predecessor.revision
            backward_doc["release_tree"] = self.predecessor.tree
            backward_path = directory / "LOCAL-backward-parser-fixture.json"
            backward_path.write_text(json.dumps(backward_doc))
            os.chmod(backward_path, 0o600)
            predecessor_id = json.loads(
                self.docker("image", "inspect", self.predecessor.reference)
            )[0]["Id"]
            backward_binding = replace(
                binding,
                candidate_revision=self.predecessor.revision,
                candidate_digest=self.predecessor.digest,
                candidate_image_id=predecessor_id,
            )
            backward_context = replace(
                context,
                target_revision=self.predecessor.revision,
                target_digest=self.predecessor.digest,
                target_image_id=predecessor_id,
                production_authorization_path=backward_path,
            )
            expect_refusal(
                lambda: migration.validate_maintenance_admission(
                    backward_context,
                    backward_binding,
                ),
                code="maintenance_source_transition_unproved",
            )
            self.outcomes.append(
                ScenarioOutcome(
                    "backward-maintenance-refused", "passed", "real-predecessor-target"
                )
            )
            # Same digest retry uses the actual installed floor and controller.
            repaired_binding, before, heads = migration.prepare_maintenance_context(
                context
            )
            if known_predecessor and before != record.heads_before:
                raise AcceptanceRefused("known_predecessor_start_heads_changed")
            result = migration.run_migration(
                deploy_directory=directory,
                binding_json=json.dumps(repaired_binding.public_json()),
                run_id=context.deployment_run_id,
                authorization_run_id=context.authorization_run_id,
                target_revision=context.target_revision,
                candidate_digest=context.target_digest,
                heads_before=before,
                candidate_heads=heads,
                budget_seconds=120,
                maintenance_context=parsed,
            )
            if result != 0:
                raise AcceptanceRefused("same_repair_failed")
            repaired, _digest = migration._load_journal(
                migration._journal_path(directory)
            )
            archive = (
                migration._journal_path(directory).parent
                / "history"
                / f"{record.identity.invocation_id}.json"
            )
            archived, _archive_digest = migration._load_journal(archive)
            if (
                repaired is None
                or repaired.phase is not migration.MigrationPhase.COMPLETE
                or repaired.identity.invocation_id == record.identity.invocation_id
                or repaired.identity.container_id == record.identity.container_id
                or archived != record
                or not migration._exact_container_absence_proven(repaired.identity)
            ):
                raise AcceptanceRefused(
                    "same_repair_new_invocation_or_archive_unproved"
                )
            if known_predecessor:
                retained = migration._inspect_exact(
                    record.identity,
                    binding,
                    expected_status="exited",
                    runtime_config=probe._verify(binding, allow_stopped_app=True),
                )
                if (
                    migration._terminal(record.identity, binding, retained)
                    != record.terminal
                ):
                    raise AcceptanceRefused("failed_runner_was_restarted_or_changed")
                self.outcomes.append(
                    ScenarioOutcome(
                        "failed691-known-predecessor-SAME-new-invocation",
                        "passed",
                        "real-lock-failure-crash-exited",
                        ("exited", "forward-repair-required", "complete"),
                        repaired.identity.invocation_id,
                        repaired.identity.container_id,
                        record.heads_before,
                        heads,
                        f"basis={admission.basis.value}; email_floor=installed; payments_start_floor=predecessor; archived_failed_invocation={record.identity.invocation_id}; retained_failed_container={record.identity.container_id}",
                    )
                )
            self.outcomes.append(
                ScenarioOutcome(
                    "known-predecessor-SAME-maintenance-FD7-FD8"
                    if known_predecessor
                    else "installed-candidate-SAME-maintenance-FD7-FD8",
                    "passed",
                    "none",
                    ("complete",),
                    record.identity.invocation_id,
                    record.identity.container_id,
                    record.heads_before,
                    heads,
                    "LOCAL parser/control fixture; no deployment authority",
                )
            )
            # The floor owner must reject actual unknown schema, never infer
            # admission merely from Alembic heads.
            self.sql(
                "ALTER TABLE public.topup_intents DROP COLUMN module_intent_id CASCADE",
                database=f"migration_ci_{self.counter}",
            )

            def observe_unknown_floor() -> None:
                floor = probe._probe(
                    binding,
                    image="candidate",
                    module="scripts.verify_payments_intent_rollback",
                    arguments=("inspect",),
                    operation="payments-installed",
                )
                probe._validate_payments_floor_output("payments-installed", floor)

            expect_refusal(observe_unknown_floor)
            self.outcomes.append(
                ScenarioOutcome(
                    "unknown-installed-floor-refused", "passed", "actual-column-drift"
                )
            )
        finally:
            if database_lock is not None:
                self.release_database_lock(database_lock)
            if context_fd is not None:
                os.close(context_fd)
            if lock_fd is not None:
                os.close(lock_fd)
            pending, _pending_digest = migration._load_journal(
                migration._journal_path(directory)
            )
            if pending is not None:
                self.remember_runner(pending)
            if "record" in locals() and record is not None:
                self.remember_runner(record)
            if known_predecessor and "record" in locals() and record is not None:
                if record.identity.container_id in self.containers:
                    self.docker("rm", "-f", record.identity.container_id)
                    self.containers.remove(record.identity.container_id)
            self.finish_fixture(binding)

    def drift_canaries(self) -> None:
        directory, binding = self.fixture()
        try:
            probe.verify_fixed_compose_inventory(binding)
            extra = self.create(
                "--network",
                self.network or "",
                "--entrypoint",
                "/opt/venv/bin/python",
                self.candidate.reference,
                "-c",
                "import time; time.sleep(60)",
            )
            self.docker("start", extra)
            expect_refusal(
                lambda: probe.verify_fixed_compose_inventory(binding),
                code="compose_inventory_changed",
            )
            self.docker("rm", "-f", extra)
            self.containers.remove(extra)
            worker = binding.writers[0].container_id
            self.docker("start", worker)
            expect_refusal(
                lambda: probe._verify(binding, allow_stopped_app=True),
                code="writer_identity_changed",
            )
            self.docker("stop", worker)
            self.outcomes.append(
                ScenarioOutcome(
                    "extra-and-restarted-writer-refused",
                    "passed",
                    "actual-writer-start",
                )
            )
        finally:
            self.finish_fixture(binding)
        for kind in ("endpoint", "network", "critical-host"):
            directory, binding = self.fixture()
            try:
                if kind == "endpoint":
                    self.docker("restart", self.broker)
                    expect_refusal(
                        lambda binding=binding: probe._verify(
                            binding, allow_stopped_app=True
                        ),
                        code="endpoint_identity_changed",
                    )
                elif kind == "network":
                    self.docker(
                        "network",
                        "disconnect",
                        self.network or "",
                        binding.app_container_id,
                    )
                    expect_refusal(
                        lambda binding=binding: probe._verify(
                            binding, allow_stopped_app=True
                        ),
                        code="app_network_changed",
                    )
                else:
                    # Real Compose drift overrides a critical database name.
                    override = directory / "docker-compose.override.yml"
                    override.write_text(
                        "services:\n  app:\n    extra_hosts:\n      postgres-local: 127.0.0.1\n"
                    )
                    drift = replace(
                        binding,
                        compose_arguments=(
                            *binding.compose_arguments,
                            "-f",
                            str(override),
                        ),
                    )
                    expect_refusal(
                        lambda drift=drift: probe._verify(
                            drift, allow_stopped_app=True
                        ),
                        code="critical_name_override",
                    )
                self.outcomes.append(
                    ScenarioOutcome(
                        f"{kind}-drift-refused", "passed", f"actual-{kind}-drift"
                    )
                )
            finally:
                self.finish_fixture(binding)
        malformed = self.directory / "malformed-fd"
        malformed.write_text("{}")
        descriptor = os.open(malformed, os.O_RDWR)
        try:
            expect_refusal(
                lambda: migration.read_maintenance_context(descriptor),
                code="maintenance_context_unproved",
            )
        finally:
            os.close(descriptor)
        self.outcomes.append(
            ScenarioOutcome(
                "malformed-maintenance-FD-refused", "passed", "linked-writable-FD"
            )
        )

    def supervisor_canaries(self) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            marker = self.directory / f"supervisor-{signum}"
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "scripts.ci.deployment_migration_fault_child",
                    "--supervise-marker",
                    str(marker),
                ],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                for _attempt in range(100):
                    if marker.exists():
                        break
                    if child.poll() is not None:
                        raise AcceptanceRefused("supervisor_child_failed_before_signal")
                    time.sleep(0.1)
                else:
                    raise AcceptanceRefused("supervisor_fd8_proof_missing")
                child.send_signal(signum)
                if child.wait(timeout=20) == 0:
                    raise AcceptanceRefused("supervisor_signal_reported_success")
                descriptor = trusted._acquire_canonical_lock()
                os.close(descriptor)
                self.outcomes.append(
                    ScenarioOutcome(
                        f"supervisor-{signal.Signals(signum).name}-FD8",
                        "passed",
                        "actual-signal",
                    )
                )
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)

    def run(self) -> None:
        self.prepare()
        tasks: list[tuple[str, Callable[[], None]]] = [
            ("real-predecessor-upgrade", lambda: self.scenario("none")),
            ("real-lock-nonzero", lambda: self.scenario("none", hold_lock=True)),
            (
                "timeout-retained",
                lambda: self.scenario("none", hold_lock=True, budget=1),
            ),
        ]
        for fault in ("ack-create", "ack-start", "ack-remove"):
            tasks.append((fault, lambda fault=fault: self.scenario(fault)))
        for phase in (
            "create-requested",
            "created",
            "start-requested",
            "running",
            "exited",
            "remove-requested",
            "removed",
            "complete",
        ):
            tasks.append(
                (f"crash-{phase}", lambda phase=phase: self.scenario(f"crash-{phase}"))
            )
        tasks.extend(
            (
                ("drift-canaries", self.drift_canaries),
                ("maintenance-canaries", self.maintenance_canaries),
                (
                    "known-predecessor-SAME-repair",
                    lambda: self.maintenance_canaries(known_predecessor=True),
                ),
                ("supervisor-canaries", self.supervisor_canaries),
            )
        )
        for name, action in tasks:
            try:
                action()
            except (
                AcceptanceRefused,
                migration.MigrationRefused,
                probe.ProbeRefused,
                trusted.TrustedDeploymentRefusal,
                OSError,
                ValueError,
                subprocess.SubprocessError,
            ) as exc:
                self.outcomes.append(
                    ScenarioOutcome(
                        name,
                        "failed",
                        name,
                        detail=str(exc)
                        if isinstance(exc, AcceptanceRefused)
                        else "real_owner_scenario_refused",
                    )
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--candidate-evidence", type=Path, required=True)
    parser.add_argument("--predecessor-evidence", type=Path, required=True)
    parser.add_argument("--predecessor-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require_hosted_dispatch(dict(os.environ))
    evidence = read_candidate_evidence(args.candidate_evidence)
    candidate = ImageIdentity(
        args.source_revision,
        args.source_tree,
        args.image_digest,
        evidence.build_run_id.value,
    )
    baseline = json.loads(BASELINE.read_text())
    predecessor = ImageIdentity(
        baseline["source_revision"],
        baseline["source_tree"],
        baseline["image_digest"],
        baseline["build_run_id"],
    )
    with tempfile.TemporaryDirectory(prefix="dotmac-migration-ci-") as temporary:
        harness = HostedAcceptance(
            directory=Path(temporary) / "fixtures",
            candidate=candidate,
            predecessor=predecessor,
            predecessor_source=args.predecessor_source.resolve(),
            candidate_evidence=args.candidate_evidence,
            predecessor_evidence=args.predecessor_evidence,
        )
        failure: str | None = None
        try:
            harness.run()
            if any(outcome.status == "failed" for outcome in harness.outcomes):
                failure = "required_real_acceptance_scenario_failed"
        except (
            AcceptanceRefused,
            migration.MigrationRefused,
            probe.ProbeRefused,
            OSError,
            ValueError,
            subprocess.SubprocessError,
        ):
            failure = "required_real_acceptance_scenario_failed"
        finally:
            cleanup_proved = harness.cleanup()
            if not cleanup_proved:
                failure = "fixture_cleanup_unproved"
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(
                    {
                        "schema": "DotmacHostedMigrationAcceptanceV1",
                        "scope": "development_migration_and_control_only",
                        "candidate": asdict(candidate),
                        "predecessor": asdict(predecessor),
                        "predecessor_production_health": "UNVERIFIED",
                        "fixture_database_container_id": harness.database,
                        "fixture_broker_container_id": harness.broker,
                        "fixture_network_id": harness.network,
                        "fixture_cleanup_proved": cleanup_proved,
                        "scenarios": [asdict(outcome) for outcome in harness.outcomes],
                        "failure": failure,
                        "unproved": [
                            "positive_SAFE_PREDDL_requires_real_pre_email638_and_pre691_image",
                            "FORWARD_requires_second_real_descendant_image",
                            "full_deploy_wrapper_backup_startup_health_restoration_requires_authorized_staging_rehearsal",
                        ],
                    },
                    indent=2,
                )
                + "\n"
            )
        return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
