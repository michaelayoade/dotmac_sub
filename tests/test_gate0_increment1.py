"""Increment-1 guards: job-scoped OIDC permissions, OpenBao goldens, apply CLI.

These are source-shape tests (repo deploy-test pattern): they prove the
committed workflow/policy text carries exactly the trust grants the design
allows, and that apply.py refuses unsafe addresses without any network.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from deploy.openbao import apply as apply_cli

ROOT = Path(__file__).resolve().parents[1]
RC = ROOT / ".github/workflows/release-candidate.yml"
PROOF = ROOT / ".github/workflows/openbao-jwt-proof.yml"
OPENBAO = ROOT / "deploy/openbao"
GOLDEN = ROOT / "tests/gate0_golden"
POLICIES = ("sub-staging-ci", "sub-production-ci", "gate0-proof")
NETWORK_CLAIMS = {"ip", "cidr", "source_ip", "remote_addr", "client_ip"}


GH_VERIFY_PINS = (
    "--repo michaelayoade/dotmac_sub",
    "--cert-identity",
    "--source-ref refs/heads/main",
)
CERT_IDENTITY = (
    "https://github.com/michaelayoade/dotmac_sub/.github/workflows/"
    "release-candidate.yml@refs/heads/main"
)
IDENTITY_PIN = (
    r"^https://github\.com/michaelayoade/dotmac_sub/\.github/workflows/"
    r"release-candidate\.yml@refs/heads/main$"
)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _apply_module():
    spec = importlib.util.spec_from_file_location("gate0_apply", OPENBAO / "apply.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_candidate_signing_permissions_are_build_job_only() -> None:
    doc = _load(RC)
    assert "id-token" not in doc.get("permissions", {})
    assert "attestations" not in doc.get("permissions", {})
    holders = {
        name
        for name, job in doc["jobs"].items()
        if job.get("permissions", {}).get("id-token") == "write"
    }
    assert holders == {"build"}
    attestors = {
        name
        for name, job in doc["jobs"].items()
        if job.get("permissions", {}).get("attestations") == "write"
    }
    assert attestors == {"build"}
    # The job block restates (never widens beyond) workflow permissions.
    assert doc["jobs"]["build"]["permissions"] == {
        "actions": "read",
        "contents": "read",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }


def test_release_candidate_signing_actions_are_sha_pinned() -> None:
    source = RC.read_text(encoding="utf-8")
    for action in ("sigstore/cosign-installer", "actions/attest-build-provenance"):
        refs = re.findall(rf"uses: {re.escape(action)}@([0-9a-f]{{40}})", source)
        assert refs, action
        assert not re.search(rf"uses: {re.escape(action)}@v\d", source), action


def test_proof_workflow_id_token_only_on_minting_jobs() -> None:
    doc = _load(PROOF)
    assert "id-token" not in doc.get("permissions", {})
    assert "attestations" not in str(doc)
    minting = {
        name
        for name, job in doc["jobs"].items()
        if job.get("permissions", {}).get("id-token") == "write"
    }
    assert minting == set(doc["jobs"]) - {"verdict"}
    assert "permissions" not in doc["jobs"]["verdict"]
    assert doc["jobs"]["positive"]["environment"] == "staging"
    assert doc["jobs"]["row3-no-environment"].get("environment") is None


def test_proof_openbao_jobs_run_on_staging_self_hosted_runner() -> None:
    # D3: OpenBao is reached from the self-hosted staging runner over the
    # private app path; only the verdict job (no OpenBao contact) stays hosted.
    doc = _load(PROOF)
    for name, job in doc["jobs"].items():
        if name == "verdict":
            assert job["runs-on"] == "ubuntu-latest"
        else:
            assert job["runs-on"] == [
                "self-hosted",
                "linux",
                "x64",
                "dotmac-sub-staging",
            ], name


def test_proof_workflow_is_dispatch_only_with_pass_verdict() -> None:
    source = PROOF.read_text(encoding="utf-8")
    triggers = _load(PROOF).get(True) or _load(PROOF).get("on")
    assert set(triggers) == {"workflow_dispatch"}
    assert 'echo "GATE0 PROOF 1: PASS"' in source
    assert "vault-action" not in source


@pytest.mark.parametrize("name", POLICIES)
def test_policy_hcl_matches_golden(name: str) -> None:
    actual = (OPENBAO / "policies" / f"{name}.hcl").read_text(encoding="utf-8")
    golden = (GOLDEN / f"{name}.hcl.golden").read_text(encoding="utf-8")
    assert actual == golden
    paths = re.findall(r'path "([^"]+)"', actual)
    assert paths and all("*" not in path and "+" not in path for path in paths)


def test_roles_json_matches_golden_and_never_binds_network() -> None:
    actual = json.loads((OPENBAO / "roles.json").read_text(encoding="utf-8"))
    golden = json.loads((GOLDEN / "roles.json.golden").read_text(encoding="utf-8"))
    assert actual == golden
    assert actual["default_role"] == ""
    assert set(actual["roles"]) == set(POLICIES)
    for role in actual["roles"].values():
        assert role["bound_audiences"] == ["dotmac-openbao"]
        assert "token_num_uses" not in role
        assert "token_bound_cidrs" not in role
        assert NETWORK_CLAIMS.isdisjoint(role["bound_claims"])
        assert role["bound_claims"]["repository_id"] == ["1163746040"]
        assert role["bound_claims"]["repository_owner_id"] == ["32591929"]
        assert set(role["bound_claims"]) == {
            "repository",
            "repository_owner",
            "repository_id",
            "repository_owner_id",
            "ref",
            "environment",
            "job_workflow_ref",
        }
    assert actual["roles"]["gate0-proof"]["token_ttl"] == "10m"
    for name in ("sub-staging-ci", "sub-production-ci"):
        role = actual["roles"][name]
        assert (role["token_ttl"], role["token_max_ttl"]) == ("20m", "60m")


@pytest.mark.parametrize(
    "address",
    [
        "http://openbao.example.com:8200",
        "http://10.0.0.5:8200",
        "ftp://127.0.0.1:8200",
        "https://127.0.0.1:8200/path",
        "https://user:pass@127.0.0.1:8200",
        "not-a-url",
    ],
)
def test_apply_refuses_unsafe_openbao_addresses(address: str) -> None:
    module = _apply_module()
    with pytest.raises(module.ApplyError):
        module.validated_openbao_address(address)


@pytest.mark.parametrize(
    "address",
    ["http://127.0.0.1:8200", "http://localhost:8200", "https://bao.example.com"],
)
def test_apply_accepts_safe_openbao_addresses(address: str) -> None:
    assert _apply_module().validated_openbao_address(address) == address


def test_apply_dry_run_prints_exact_writes_and_no_secret_values() -> None:
    env = {key: value for key, value in os.environ.items() if key != "OPENBAO_ADDR"}
    result = subprocess.run(
        [
            sys.executable,
            str(OPENBAO / "apply.py"),
            "--dry-run",
            "--addr",
            "http://127.0.0.1:8200",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "INITIALIZE-IF-ABSENT PUT /v1/auth/jwt/config" in out
    for name in POLICIES:
        assert f"WRITE PUT /v1/sys/policies/acl/{name}" in out
        assert f"WRITE PUT /v1/auth/jwt/role/{name}" in out
    assert "SEED-IF-ABSENT POST /v1/secret/data/dotmac/sub/staging/gate0-canary" in out
    assert (
        "SEED-IF-ABSENT POST /v1/secret/data/dotmac/sub/production/gate0-negative"
        in out
    )
    assert "<generated, never printed>" in out


def test_apply_dry_run_refuses_non_loopback_http() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(OPENBAO / "apply.py"),
            "--dry-run",
            "--addr",
            "http://openbao.example.com:8200",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert "loopback" in result.stderr


def test_gate0_scripts_are_posix_and_value_free() -> None:
    for script in ("proof1_oidc_openbao.sh", "verify_artifact.sh"):
        path = ROOT / "deploy/gate0" / script
        result = subprocess.run(
            ["sh", "-n", str(path)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        assert path.read_text(encoding="utf-8").startswith("#!/bin/sh")
    verify = (ROOT / "deploy/gate0/verify_artifact.sh").read_text(encoding="utf-8")
    assert "--insecure-ignore-tlog" not in verify
    assert "verify-attestation" not in verify
    assert "slsaprovenance" not in verify
    assert 'gh attestation verify "oci://$REF"' in verify
    for pin in GH_VERIFY_PINS:
        assert pin in verify, pin


def test_proof_workflow_uses_secret_for_openbao_addr_never_a_variable() -> None:
    source = PROOF.read_text(encoding="utf-8")
    assert "vars.OPENBAO_ADDR" not in source
    assert source.count("secrets.OPENBAO_ADDR") == 7
    readme = (OPENBAO / "README.md").read_text(encoding="utf-8")
    assert "repository variable `OPENBAO_ADDR`" not in readme
    assert "repository **secret**" in readme


def test_public_docs_do_not_describe_openbao_listener_posture() -> None:
    for path in (PROOF, OPENBAO / "README.md"):
        text = path.read_text(encoding="utf-8").lower()
        for phrase in (
            "plaintext",
            "probe timed",
            "internet reachab",
            "not reachable from",
        ):
            assert phrase not in text, (path.name, phrase)


def _proof_guard(address: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "OPENBAO_ADDR": address,
        "PROOF_MODE": "negative",
        "PROOF_ROLE": "gate0-proof",
    }
    return subprocess.run(
        ["sh", str(ROOT / "deploy/gate0/proof1_oidc_openbao.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@pytest.mark.parametrize(
    "address",
    [
        "http://localhost.evil.tld",
        "http://localhost.evil.tld:8200",
        "http://127.0.0.1.evil.tld",
        "http://127.0.0.1:80@evil.example.com",
        "http://localhost@evil.example.com",
        "http://10.0.0.5:8200",
        "http://localhost:abc",
    ],
)
def test_proof_script_localhost_check_is_exact(address: str) -> None:
    result = _proof_guard(address)
    assert result.returncode == 2
    assert "refusing" in result.stderr


@pytest.mark.parametrize(
    "address",
    [
        "http://localhost:8200",
        "http://127.0.0.1:8200/",
        "http://[::1]:8200",
        "https://openbao.example.com",
    ],
)
def test_proof_script_accepts_exact_loopback_and_https(address: str) -> None:
    result = _proof_guard(address)
    # Passes the address guard; stops at the OIDC env check (exit 2, no network).
    assert "refusing" not in result.stderr
    assert "OIDC request env absent" in result.stderr


def test_proof_script_keeps_secrets_off_curl_argv() -> None:
    text = (ROOT / "deploy/gate0/proof1_oidc_openbao.sh").read_text(encoding="utf-8")
    assert "X-Vault-Token: $TOKEN" not in text
    assert "bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" not in text
    assert 'jwt\\":\\"$JWT' not in text
    assert "curl -K -" in text and "--data @-" in text
    # The JWT must not be on any argv, jq included (it travels via env/stdin).
    assert "--arg jwt" not in text
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if "jq " in line or "curl " in line:
            assert "$JWT" not in line.replace('JWT="$JWT" jq', "jq"), line
    assert 'JWT="$JWT" jq -nc' in text and "jwt:env.JWT" in text


def _rc_steps() -> list[dict]:
    return _load(RC)["jobs"]["build"]["steps"]


def test_release_candidate_provenance_uses_gh_attestation_not_cosign() -> None:
    source = RC.read_text(encoding="utf-8")
    # attest-build-provenance stores a Sigstore bundle, not a cosign .att.
    assert "slsaprovenance" not in source
    assert "verify-attestation" not in source
    assert "--type slsaprovenance" not in source
    verify = [s for s in _rc_steps() if "gh attestation verify" in s.get("run", "")]
    assert len(verify) == 1
    step = verify[0]
    run = step["run"]
    assert 'gh attestation verify "oci://$ref"' in run
    for pin in GH_VERIFY_PINS:
        assert pin in run, pin
    # cosign signature check stays, identity + issuer pinned.
    assert "cosign verify --certificate-identity-regexp" in run
    assert IDENTITY_PIN in run
    assert (
        "--certificate-oidc-issuer https://token.actions.githubusercontent.com" in run
    )
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    # gh is the runner's preinstalled CLI, not installed here.
    assert "gh/install" not in source and "setup-gh" not in source


def test_release_candidate_reuse_mode_runs_the_same_verification() -> None:
    steps = _rc_steps()
    names = [s.get("name") for s in steps]
    name = "Verify signature and provenance (build and reuse)"
    assert names.count(name) == 1
    step = steps[names.index(name)]
    # Not gated on mode: runs for build AND reuse, with no skip path.
    assert "if" not in step
    assert "continue-on-error" not in step
    assert step["env"]["IMAGE_DIGEST"] == "${{ steps.accepted.outputs.image_digest }}"
    # Runs after the accepted digest is resolved, before evidence is produced.
    assert names.index("Resolve accepted candidate digest") < names.index(name)
    assert names.index(name) < names.index("Write typed candidate evidence")
    # cosign is installed for both modes and before verification.
    install = [i for i, s in enumerate(steps) if s.get("name") == "Install cosign"]
    assert len(install) == 1 and "if" not in steps[install[0]]
    assert install[0] < names.index(name)
    # A failure fails the job (no `|| true`, no swallowed exit).
    assert "|| true" not in step["run"]
    assert "exit 1" in step["run"]


def test_release_candidate_attest_step_pinned_and_push_to_registry() -> None:
    steps = {s.get("name"): s for s in _rc_steps()}
    attest = steps["Attest build provenance"]
    assert re.fullmatch(r"actions/attest-build-provenance@[0-9a-f]{40}", attest["uses"])
    assert attest["with"]["push-to-registry"] is True
    for name in ("Install cosign",):
        assert re.fullmatch(
            r"sigstore/cosign-installer@[0-9a-f]{40}", steps[name]["uses"]
        )


def _run_proof_negative(
    tmp_path: Path, http_code: str, body: str
) -> subprocess.CompletedProcess[str]:
    """Run the negative proof with stubbed curl/jq-free network (no real I/O)."""
    import shutil

    if shutil.which("jq") is None:
        pytest.skip("jq not installed")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "body.json").write_text(body, encoding="utf-8")
    # First curl call mints the JWT (-K -), second is the login (-o FILE).
    stub = bindir / "curl"
    stub.write_text(
        "#!/bin/sh\n"
        "out=''\n"
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in -o) out=$2; shift ;; esac\n'
        "  shift\n"
        "done\n"
        "cat >/dev/null\n"
        'if [ -n "$out" ]; then\n'
        f'  cat {tmp_path}/body.json > "$out"\n'
        f"  printf '%s' '{http_code}'\n"
        "  [ '" + http_code + "' = 000 ] && exit 7\n"
        "  exit 0\n"
        "fi\n"
        'printf \'{"value":"x"}\'\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    env = {
        "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "OPENBAO_ADDR": "https://openbao.example.com",
        "PROOF_MODE": "negative",
        "PROOF_ROLE": "sub-staging-ci",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.example.com/?a=b",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "t",
    }
    return subprocess.run(
        ["sh", str(ROOT / "deploy/gate0/proof1_oidc_openbao.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@pytest.mark.parametrize(
    ("code", "body"),
    [
        (
            "400",
            '{"errors":["error validating claims: claim \\"ref\\" does not match"]}',
        ),
        ("403", '{"errors":["permission denied"]}'),
    ],
)
def test_proof_negative_passes_only_on_definite_login_refusal(
    tmp_path, code, body
) -> None:
    result = _run_proof_negative(tmp_path, code, body)
    assert result.returncode == 0, result.stderr
    assert "negative OK" in result.stdout


@pytest.mark.parametrize(
    ("code", "body"),
    [
        ("000", ""),  # network error
        ("404", '{"errors":[]}'),
        ("502", "<html>bad gateway</html>"),
        ("503", '{"errors":["Vault is sealed"]}'),
        ("400", "not json"),
        ("400", '{"errors":[]}'),
        ("400", '{"errors":["role \\"sub-staging-ci\\" could not be found"]}'),
        ("200", '{"auth":{"client_token":"s.x"}}'),
    ],
)
def test_proof_negative_is_not_a_pass_for_unrelated_errors(
    tmp_path, code, body
) -> None:
    result = _run_proof_negative(tmp_path, code, body)
    assert result.returncode == 1, (code, body, result.stdout)
    assert "negative OK" not in result.stdout


def test_provenance_verify_uses_exact_cert_identity_and_source_digest() -> None:
    workflow = RC.read_text(encoding="utf-8")
    script = (ROOT / "deploy/gate0/verify_artifact.sh").read_text(encoding="utf-8")
    for text in (workflow, script):
        assert f"'{CERT_IDENTITY}'" in text
        assert "--signer-workflow" not in text
        assert "--predicate-type" not in text  # default is SLSA provenance v1
    assert '--cert-identity "$cert_identity"' in workflow
    assert '--source-digest "$SOURCE_REVISION"' in workflow
    assert "SOURCE_REVISION: ${{ steps.candidate.outputs.sha }}" in workflow
    assert "--deny-self-hosted-runners" not in workflow
    assert "--deny-self-hosted-runners" in script
    assert '--cert-identity "$CERT_IDENTITY"' in script
    assert "deleting this GHCR package version" in workflow


def test_proof_script_login_temp_file_is_under_runner_temp() -> None:
    text = (ROOT / "deploy/gate0/proof1_oidc_openbao.sh").read_text(encoding="utf-8")
    assert "RUNNER_TEMP" in text and "mktemp -d" in text
    assert "LOGIN_BODY=$(mktemp)" not in text


class ApplyTransport:
    """Mock HTTP only; real typed apply plan and preflight outcomes stay in use."""

    def __init__(self) -> None:
        module = apply_cli
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []
        config = module.planned_writes(
            json.loads((OPENBAO / "roles.json").read_text(encoding="utf-8"))
        ).config
        self.responses: dict[str, apply_cli.HttpResponse] = {
            "/v1/sys/internal/ui/mounts/secret": module.HttpResponse(
                200, {"data": {"type": "kv", "options": {"version": "2"}}}
            ),
            "/v1/sys/auth": module.HttpResponse(
                200, {"data": {"jwt/": {"type": "jwt"}}}
            ),
            "/v1/auth/jwt/config": module.HttpResponse(200, {"data": config.body()}),
        }
        for leaf in module.CANARIES:
            self.responses[f"/v1/{leaf}"] = module.HttpResponse(404, {})
        self.seed_status = 200

    def request(
        self,
        addr: str,
        token: str,
        method: str,
        path: str,
        body: dict[str, object] | None,
    ) -> apply_cli.HttpResponse:
        self.calls.append((method, path, body))
        if method == "GET":
            return self.responses[path]
        return apply_cli.HttpResponse(self.seed_status if method == "POST" else 204, {})

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(apply_cli, "_request", self.request)
        # Deliberately empty: the mock never authenticates or contacts a service.
        monkeypatch.setenv("OPENBAO_TOKEN", "test-only-placeholder")

    def run(self) -> int:
        return apply_cli.main(["--addr", "http://127.0.0.1:8200"])


@pytest.mark.parametrize(
    "field", ["oidc_discovery_url", "bound_issuer", "default_role"]
)
def test_apply_config_drift_refuses_before_all_writes(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    module = apply_cli
    transport = ApplyTransport()
    config = module.planned_writes(
        json.loads((OPENBAO / "roles.json").read_text(encoding="utf-8"))
    ).config.body()
    config[field] = "different"
    transport.responses["/v1/auth/jwt/config"] = module.HttpResponse(
        200, {"data": config}
    )
    transport.install(monkeypatch)
    assert transport.run() == 1
    assert all(method == "GET" for method, _, _ in transport.calls)


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (403, {}),
        (500, {}),
        (302, {}),
        (204, {}),
        (200, {}),
        (200, {"data": {"data": {"value": ""}}}),
        (200, {"data": {"data": {"value": 3}}}),
        (200, {"data": []}),
    ],
)
def test_apply_uncertain_second_canary_refuses_before_all_writes(
    monkeypatch: pytest.MonkeyPatch, status: int, payload: object
) -> None:
    module = apply_cli
    transport = ApplyTransport()
    transport.responses[f"/v1/{module.CANARIES[1]}"] = module.HttpResponse(
        status, payload
    )
    transport.install(monkeypatch)
    assert transport.run() == 1
    assert all(method == "GET" for method, _, _ in transport.calls)
    assert all(
        f"/v1/{leaf}" in [p for _, p, _ in transport.calls] for leaf in module.CANARIES
    )


@pytest.mark.parametrize(
    ("path", "status", "payload"),
    [
        ("/v1/sys/auth", 200, {"data": {}}),
        ("/v1/sys/auth", 200, {"data": {"jwt/": {"type": "oidc"}}}),
        ("/v1/sys/auth", 403, {}),
        ("/v1/auth/jwt/config", 403, {}),
        ("/v1/auth/jwt/config", 503, {}),
        ("/v1/auth/jwt/config", 200, {"data": {}}),
        (
            "/v1/sys/internal/ui/mounts/secret",
            200,
            {"data": {"type": "kv", "options": {"version": "1"}}},
        ),
    ],
)
def test_apply_mount_or_config_uncertainty_refuses_before_all_writes(
    monkeypatch: pytest.MonkeyPatch, path: str, status: int, payload: object
) -> None:
    module = apply_cli
    transport = ApplyTransport()
    transport.responses[path] = module.HttpResponse(status, payload)
    transport.install(monkeypatch)
    assert transport.run() == 1
    assert all(method == "GET" for method, _, _ in transport.calls)


def test_apply_preserves_existing_config_and_canary_creates_only_absent_with_cas(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = apply_cli
    transport = ApplyTransport()
    existing = "existing-test-canary"
    transport.responses[f"/v1/{module.CANARIES[0]}"] = module.HttpResponse(
        200, {"data": {"data": {"value": existing}}}
    )
    transport.install(monkeypatch)
    assert transport.run() == 0
    reads = [path for method, path, _ in transport.calls if method == "GET"]
    assert reads[-2:] == [f"/v1/{leaf}" for leaf in module.CANARIES]
    assert all(method == "GET" for method, _, _ in transport.calls[:5])
    writes = [
        (method, path, body)
        for method, path, body in transport.calls
        if method != "GET"
    ]
    assert len(writes) == 7
    assert all(path != "/v1/auth/jwt/config" for _, path, _ in writes)
    seeds = [(path, body) for method, path, body in writes if method == "POST"]
    assert len(seeds) == 1
    path, body = seeds[0]
    assert path == f"/v1/{module.CANARIES[1]}"
    assert body is not None and body["options"] == {"cas": 0}
    output = capsys.readouterr().out
    assert existing not in output
    assert "canary present (unchanged)" in output


def test_apply_absent_config_initializes_only_after_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = apply_cli
    transport = ApplyTransport()
    transport.responses["/v1/auth/jwt/config"] = module.HttpResponse(404, {})
    transport.install(monkeypatch)
    assert transport.run() == 0
    assert transport.calls[5][:2] == ("PUT", "/v1/auth/jwt/config")
    assert all(method == "GET" for method, _, _ in transport.calls[:5])


def test_apply_canary_cas_conflict_has_no_overwrite_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = apply_cli
    transport = ApplyTransport()
    transport.seed_status = 400
    transport.install(monkeypatch)
    assert transport.run() == 1
    seeds = [(path, body) for method, path, body in transport.calls if method == "POST"]
    assert len(seeds) == 1
    assert seeds[0][1] is not None and seeds[0][1]["options"] == {"cas": 0}


def test_apply_dry_run_never_calls_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    module = apply_cli
    transport = ApplyTransport()
    transport.install(monkeypatch)
    assert module.main(["--dry-run", "--addr", "http://127.0.0.1:8200"]) == 0
    assert transport.calls == []


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_apply_transport_refuses_redirect_without_following(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """Exercise the real opener/error pipeline with an in-memory HTTP handler."""
    import io
    import urllib.request
    from email.message import Message
    from urllib.response import addinfourl

    requests: list[urllib.request.Request] = []

    class RedirectResponse(urllib.request.HTTPHandler):
        def http_open(self, req: urllib.request.Request) -> addinfourl:
            requests.append(req)
            assert (
                req.full_url == "http://127.0.0.1:8200/v1/sys/internal/ui/mounts/secret"
            )
            headers = Message()
            headers["Location"] = "http://different-host.example/redirect-target"
            response = addinfourl(io.BytesIO(b""), headers, req.full_url, code=status)
            response.msg = "Redirect"
            return response

    original_builder = urllib.request.build_opener

    def in_memory_opener(
        *handlers: urllib.request.BaseHandler,
    ) -> urllib.request.OpenerDirector:
        assert any(
            isinstance(handler, apply_cli.NoRedirectHandler) for handler in handlers
        )
        return original_builder(*handlers, RedirectResponse())

    monkeypatch.setattr(urllib.request, "build_opener", in_memory_opener)
    monkeypatch.setenv("OPENBAO_TOKEN", "test-only-placeholder")
    response = apply_cli._request(
        "http://127.0.0.1:8200", "", "GET", "/v1/sys/internal/ui/mounts/secret", None
    )
    assert response == apply_cli.HttpResponse(status, {})
    assert apply_cli.main(["--addr", "http://127.0.0.1:8200"]) == 1
    # One initial request per call above; neither call follows Location.
    assert len(requests) == 2
    assert all(req.get_method() == "GET" for req in requests)
