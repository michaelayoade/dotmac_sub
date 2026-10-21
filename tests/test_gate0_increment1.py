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

ROOT = Path(__file__).resolve().parents[1]
RC = ROOT / ".github/workflows/release-candidate.yml"
PROOF = ROOT / ".github/workflows/openbao-jwt-proof.yml"
OPENBAO = ROOT / "deploy/openbao"
GOLDEN = ROOT / "tests/gate0_golden"
POLICIES = ("sub-staging-ci", "sub-production-ci", "gate0-proof")
NETWORK_CLAIMS = {"ip", "cidr", "source_ip", "remote_addr", "client_ip"}


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
    assert "WRITE PUT /v1/auth/jwt/config" in out
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
    assert "verify-attestation --type slsaprovenance" in verify


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
