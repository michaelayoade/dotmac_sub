"""Host observations and one existing executor, with no inherited credentials."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeAlias
from urllib.parse import urlparse

Json: TypeAlias = None | bool | int | str | list["Json"] | dict[str, "Json"]
CONFIG_FILES = ("docker-compose.yml", "docker-compose.override.yml")
IMAGE = "ghcr.io/michaelayoade/dotmac_sub"
IDENTITY = "https://github.com/michaelayoade/dotmac_sub/.github/workflows/release-candidate.yml@refs/heads/main"
ISSUER = "https://token.actions.githubusercontent.com"
LIVE = "http://10.120.121.20:8001/health"
READY = "http://10.120.121.20:8001/api/v1/health/ready"
ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "COMPOSE_PROJECT_NAME": "dotmac_sub",
}


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def protected(path: Path, *, secret: bool = False) -> None:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != (os.geteuid() if secret else 0)
        or (stat.S_IMODE(info.st_mode) != 0o600 if secret else info.st_mode & 0o022)
    ):
        raise ValueError("unsafe_file_permissions")


@dataclass(frozen=True)
class Config:
    cp_url: str
    deploy_dir: Path
    state_dir: Path
    keyring_file: Path
    host_key_file: Path
    host_key_id: str
    artifact_digest: str
    version: str
    guarded_executor_sha256: str
    agent_lock: Path = Path("/run/dotmac-deploy-agent/agent.lock")
    deploy_lock: Path = Path("/var/lock/dotmac_sub_deploy.lock")
    poll_seconds: int = 45
    timeout_seconds: int = 2700
    gh_config_dir: Path = Path("/etc/dotmac-deploy-agent/gh")
    docker_config_dir: Path = Path("/etc/dotmac-deploy-agent/docker")
    allow_test_input: bool = False
    product: str = "dotmac_sub"
    environment: str = "staging"
    host: str = "dotmac-sub-staging"
    image_reference: str = IMAGE

    @property
    def deployment_ref(self) -> str:
        return f"{self.product}/{self.environment}/{self.host}"

    def __post_init__(self) -> None:
        url = urlparse(self.cp_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
        ):
            raise ValueError("cp_url must be an HTTPS origin")
        if (self.product, self.environment, self.host, self.image_reference) != (
            "dotmac_sub",
            "staging",
            "dotmac-sub-staging",
            IMAGE,
        ):
            raise ValueError("staging_only")
        if type(self.allow_test_input) is not bool:
            raise ValueError("invalid_test_input_switch")
        if not 30 <= self.poll_seconds <= 60 or not 1 <= self.timeout_seconds <= 2700:
            raise ValueError("invalid_time_limit")
        if not all(
            p.is_absolute()
            for p in (
                self.deploy_dir,
                self.state_dir,
                self.keyring_file,
                self.host_key_file,
                self.agent_lock,
                self.deploy_lock,
                self.gh_config_dir,
                self.docker_config_dir,
            )
        ):
            raise ValueError("absolute_paths_required")
        if any(
            len(value) != 71
            or not value.startswith("sha256:")
            or any(c not in "0123456789abcdef" for c in value[7:])
            for value in (self.artifact_digest, self.guarded_executor_sha256)
        ):
            raise ValueError("agent_artifact_digest_required")

    @classmethod
    def load(cls, path: Path) -> Config:
        if os.geteuid() == 0:
            raise ValueError("dedicated_agent_user_required")
        protected(path)
        with path.open("rb") as stream:
            values = tomllib.load(stream)
        for key in (
            "deploy_dir",
            "state_dir",
            "keyring_file",
            "host_key_file",
            "agent_lock",
            "deploy_lock",
            "gh_config_dir",
            "docker_config_dir",
        ):
            if key in values:
                values[key] = Path(values[key])
        config = cls(**values)
        protected(config.keyring_file)
        protected(config.host_key_file, secret=True)
        for directory in (config.gh_config_dir, config.docker_config_dir):
            info = directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0
                or info.st_mode & 0o022
            ):
                raise ValueError("unsafe_auth_store_permissions")
        return config


@dataclass(frozen=True)
class Execution:
    exit_code: int
    receipt_line: str | None
    interrupted: bool = False


@dataclass(frozen=True)
class Snapshot:
    running_image: str | None
    running_revision_label: str | None
    pinned_image: str | None
    pinned_git_sha: str | None
    config_files: dict[str, str]
    migration_heads: tuple[str, ...]
    health: dict[str, Json]

    def observed(self) -> dict[str, Json]:
        return {
            name: getattr(self, name)
            for name in (
                "running_image",
                "running_revision_label",
                "pinned_image",
                "pinned_git_sha",
            )
        } | {
            "config_files": dict(self.config_files),
            "migration_heads": list(self.migration_heads),
        }

    def healthy(self) -> bool:
        live, ready, background = (
            self.health[name] for name in ("live", "ready", "background")
        )
        containers = (
            background.get("containers") if isinstance(background, dict) else None
        )
        return (
            isinstance(live, dict)
            and live.get("status") == 200
            and isinstance(ready, dict)
            and ready.get("status") == 200
            and ready.get("body_status") == "ready"
            and isinstance(background, dict)
            and background.get("celery_inspect_ok") is True
            and background.get("restart_counts_zero") is True
            and isinstance(containers, dict)
            and {"app", "celery-worker"} <= set(containers)
            and all(value == "running" for value in containers.values())
        )


class HostIO:
    def __init__(self, config: Config):
        self.config = config
        self.environment = ENV | {
            "HOME": str(config.state_dir),
            "GH_CONFIG_DIR": str(config.gh_config_dir),
            "DOCKER_CONFIG": str(config.docker_config_dir),
        }

    def run(
        self, argv: list[str], *, timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                argv,
                cwd=self.config.deploy_dir,
                env=self.environment,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return subprocess.CompletedProcess(
                argv, 127, "", "host_readback_unavailable"
            )

    def request(self, path: str, body: bytes | None = None) -> bytes:
        request = urllib.request.Request(  # noqa: S310 - Config pins HTTPS
            self.config.cp_url.rstrip("/") + "/platform/deploy/" + path,
            data=body,
            headers={"Content-Type": "application/json"},
        )  # noqa: S310 - Config pins HTTPS

        # Refuse redirects: unauthenticated pull/return must remain on the pinned CP origin.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        with urllib.request.build_opener(NoRedirect).open(
            request, timeout=30
        ) as response:
            data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError("response_too_large")
            return data

    def fetch(self) -> bytes | None:
        try:
            return self.request(f"desired-state/{self.config.deployment_ref}")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise

    def post(self, body: bytes) -> bool:
        result = json.loads(self.request("applied-state", body))
        return isinstance(result, dict) and result.get("verdict") in (
            "accepted",
            "duplicate",
        )

    def observed_config(self) -> dict[str, str]:
        result = {}
        for name in CONFIG_FILES:
            path = self.config.deploy_dir / name
            if path.is_file() and not path.is_symlink():
                result[name] = digest(path.read_bytes())
        return result

    def pins(self) -> dict[str, str]:
        # Retain only named non-secret pins; never expose or store other .env values.
        result = {}
        for line in (self.config.deploy_dir / ".env").read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and key in (
                "APP_IMAGE",
                "GIT_SHA",
                "APP_ENV",
                "SERVER_NAME",
                "HEALTH_URL",
            ):
                if key in result:
                    raise ValueError("config_drift")
                result[key] = value
        return result

    def preflight(self, revision: str, running_revision: str | None) -> str | None:
        try:
            pins = self.pins()
        except (OSError, ValueError):
            return "config_drift"
        if any(
            pins.get(key) != value
            for key, value in {
                "APP_ENV": "staging",
                "SERVER_NAME": "dotmac-sub-staging",
                "HEALTH_URL": LIVE,
            }.items()
        ):
            return "config_drift"
        head = self.run(["git", "rev-parse", "HEAD"])
        tree = self.run(["git", "status", "--porcelain", "--untracked-files=no"])
        if (
            head.returncode
            or head.stdout.strip() != revision
            or tree.returncode
            or tree.stdout.strip()
        ):
            return "checkout_drift"
        if (
            not running_revision
            or self.run(
                ["git", "merge-base", "--is-ancestor", running_revision, revision]
            ).returncode
        ):
            return "rollback_digest"
        return None

    def busy(self) -> bool:
        with self.config.deploy_lock.open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(stream, fcntl.LOCK_UN)
                return False
            except BlockingIOError:
                return True

    @staticmethod
    def network_failure(stderr: str) -> bool:
        return any(
            code in stderr.lower()
            for code in (
                "connection refused",
                "connection reset",
                "no such host",
                "i/o timeout",
                "network is unreachable",
                "tls handshake timeout",
            )
        )

    def artifact(
        self, image: str, image_digest: str, source_revision: str
    ) -> str | None:
        reference = image + "@" + image_digest
        signature = self.run(
            [
                "cosign",
                "verify",
                "--certificate-identity",
                IDENTITY,
                "--certificate-oidc-issuer",
                ISSUER,
                "--output",
                "json",
                reference,
            ],
            timeout=180,
        )
        if signature.returncode:
            if self.network_failure(signature.stderr):
                return "registry_unreachable"
            return (
                "signature_missing"
                if "no signatures found" in signature.stderr.lower()
                else "cosign_verify_failed"
            )
        try:
            verified = json.loads(signature.stdout)
            if not verified or any(
                item["critical"]["image"]["docker-manifest-digest"] != image_digest
                or item["critical"]["identity"]["docker-reference"] != image
                for item in verified
            ):
                return "cosign_verify_failed"
        except (ValueError, KeyError, TypeError):
            return "cosign_verify_failed"
        provenance = self.run(
            [
                "gh",
                "attestation",
                "verify",
                "oci://" + reference,
                "--repo",
                "michaelayoade/dotmac_sub",
                "--cert-identity",
                IDENTITY,
                "--cert-oidc-issuer",
                ISSUER,
                "--source-ref",
                "refs/heads/main",
                "--deny-self-hosted-runners",
                "--format",
                "json",
            ],
            timeout=180,
        )
        if provenance.returncode:
            if self.network_failure(provenance.stderr):
                return "registry_unreachable"
            return (
                "attestation_missing"
                if any(
                    text in provenance.stderr.lower()
                    for text in ("no attestations", "not found")
                )
                else "attestation_identity_mismatch"
            )
        try:
            results = json.loads(provenance.stdout)
            statements = [item["verificationResult"]["statement"] for item in results]
            for statement in statements:
                predicate = statement["predicate"]
                build = predicate["buildDefinition"]
                source = build["externalParameters"]["workflow"]
                if not any(
                    item.get("uri")
                    == "git+https://github.com/michaelayoade/dotmac_sub@refs/heads/main"
                    and item.get("digest", {}).get("gitCommit") == source_revision
                    for item in build["resolvedDependencies"]
                ):
                    return "attestation_identity_mismatch"
                if (
                    statement["predicateType"] != "https://slsa.dev/provenance/v1"
                    or build["buildType"]
                    != "https://actions.github.io/buildtypes/workflow/v1"
                    or predicate["runDetails"]["builder"]["id"]
                    != "https://github.com/actions/runner/github-hosted"
                    or source
                    != {
                        "repository": "https://github.com/michaelayoade/dotmac_sub",
                        "ref": "refs/heads/main",
                        "path": ".github/workflows/release-candidate.yml",
                    }
                ):
                    return "attestation_identity_mismatch"
                if not any(
                    subject["name"] == image
                    and subject["digest"].get("sha256") == image_digest[7:]
                    for subject in statement["subject"]
                ):
                    return "attestation_identity_mismatch"
            return None if statements else "attestation_identity_mismatch"
        except (ValueError, KeyError, TypeError):
            return "attestation_identity_mismatch"

    def observe(self) -> Snapshot:
        try:
            pins = self.pins()
        except (OSError, ValueError):
            pins = {}
        result = self.run(["docker", "inspect", "dotmac_sub_app"])
        app = json.loads(result.stdout)[0] if result.returncode == 0 else {}
        config = app.get("Config", {})
        labels = config.get("Labels") or {}
        containers_result = self.run(
            ["docker", "compose", "ps", "--all", "--format", "json"]
        )
        try:
            objects = (
                json.loads(containers_result.stdout)
                if containers_result.stdout.startswith("[")
                else [
                    json.loads(line) for line in containers_result.stdout.splitlines()
                ]
            )
        except ValueError:
            objects = []
        states: dict[str, Json] = {item["Service"]: item["State"] for item in objects}
        restart_zero = bool(objects) and containers_result.returncode == 0
        for item in objects:
            readback = self.run(
                ["docker", "inspect", "--format", "{{.RestartCount}}", item["ID"]]
            )
            restart_zero = (
                restart_zero
                and readback.returncode == 0
                and readback.stdout.strip() == "0"
            )
        celery = self.run(
            [
                "docker",
                "exec",
                "dotmac_sub_celery_worker",
                "celery",
                "-A",
                "app.celery_app",
                "inspect",
                "ping",
                "--timeout=10",
            ],
            timeout=20,
        )
        health: dict[str, Json] = {
            "background": {
                "celery_inspect_ok": celery.returncode == 0 and "pong" in celery.stdout,
                "containers": states,
                "restart_counts_zero": restart_zero,
            }
        }
        for name, url in (("live", LIVE), ("ready", READY)):
            status, body_status = None, None
            try:
                # These endpoints are compile-time staging constants, not input.
                with urllib.request.urlopen(  # noqa: S310  # nosec B310
                    url, timeout=10
                ) as response:
                    status = response.status
                    if name == "ready":
                        body_status = json.loads(response.read(65536)).get("status")
            except (OSError, ValueError):
                pass
            endpoint: dict[str, Json] = {
                "url": url,
                "status": status,
                "checked_at": now(),
            }
            if name == "ready":
                endpoint["body_status"] = body_status
            health[name] = endpoint
        heads_result = self.run(
            ["docker", "exec", "dotmac_sub_app", "alembic", "current"]
        )
        heads = (
            tuple(
                line.split()[0]
                for line in heads_result.stdout.splitlines()
                if "(head)" in line
            )
            if heads_result.returncode == 0
            else ()
        )
        return Snapshot(
            config.get("Image"),
            labels.get("org.opencontainers.image.revision"),
            pins.get("APP_IMAGE"),
            pins.get("GIT_SHA"),
            self.observed_config(),
            heads,
            health,
        )

    def execute(
        self, image_digest: str, logfile: Path, from_image_digest: str
    ) -> Execution:
        if (
            digest((self.config.deploy_dir / "scripts/deploy.sh").read_bytes())
            != self.config.guarded_executor_sha256
        ):
            raise ValueError("guarded_executor_mismatch")
        executor_env = self.environment | {
            "DEPLOY_EXPECTED_FROM_IMAGE": IMAGE + "@" + from_image_digest
        }
        with logfile.open("xb") as output:
            os.chmod(logfile, 0o600)
            child = subprocess.Popen(
                ["/usr/bin/bash", "scripts/deploy_staging.sh", image_digest],
                cwd=self.config.deploy_dir,
                env=executor_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

            def stream_output() -> None:
                pipe = child.stdout
                if pipe is None:
                    return
                for chunk in iter(lambda: pipe.read(4096), b""):
                    output.write(chunk)
                    output.flush()
                    try:
                        os.write(sys.stderr.fileno(), chunk)
                    except OSError:
                        pass

            streamer = threading.Thread(target=stream_output, daemon=True)
            streamer.start()
            interrupted = False
            try:
                code = child.wait(timeout=self.config.timeout_seconds)
            except subprocess.TimeoutExpired:
                interrupted = True
                os.killpg(child.pid, signal.SIGTERM)
                # Give deploy.sh's rollback traps time to finish; never race a live child.
                code = child.wait()
            streamer.join()
            output.flush()
            os.fsync(output.fileno())
        receipt = None
        for line in logfile.read_text(errors="replace").splitlines():
            if line.startswith("DEPLOY RECEIPT: "):
                receipt = line
        return Execution(code, receipt, interrupted)
