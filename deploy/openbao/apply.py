#!/usr/bin/env python3
"""Apply Increment-1 OpenBao JWT roles/policies/canaries (human-run only).

Idempotent: policies/roles are PUT (converge); canaries seed only when
absent. Values are generated locally, never printed; only sha256 appears.
--dry-run contacts nothing and prints the conditional write plan.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import IO, Literal, NamedTuple
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
CANARIES = (
    "secret/data/dotmac/sub/staging/gate0-canary",
    "secret/data/dotmac/sub/production/gate0-negative",
)


class ApplyError(Exception):
    pass


def validated_openbao_address(raw: str) -> str:
    """Same rules as dotmac_erp/scripts/deploy_production.py (copied semantics)."""
    address = raw.rstrip("/")
    try:
        parsed, port = urlsplit(address), urlsplit(address).port
    except ValueError as exc:
        raise ApplyError("OpenBao address has an invalid host or port") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ApplyError("OpenBao address must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ApplyError(
            "OpenBao address must not carry credentials, a query, or a fragment"
        )
    if parsed.path not in {"", "/"}:
        raise ApplyError("OpenBao address must not carry a path")
    if parsed.scheme == "http" and parsed.hostname not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        raise ApplyError("plain HTTP is permitted only for a loopback OpenBao endpoint")
    if port is not None and not 1 <= port <= 65535:
        raise ApplyError("OpenBao address port is invalid")
    return address


class JwtConfig(NamedTuple):
    discovery_url: str
    issuer: str
    default_role: str

    def body(self) -> dict[str, str]:
        return {
            "oidc_discovery_url": self.discovery_url,
            "bound_issuer": self.issuer,
            "default_role": self.default_role,
        }


class PlannedWrite(NamedTuple):
    method: Literal["PUT", "POST"]
    path: str
    body: dict[str, object]


class ApplyPlan(NamedTuple):
    auth_mount: str
    config: JwtConfig
    writes: tuple[PlannedWrite, ...]


class HttpResponse(NamedTuple):
    status: int
    payload: object


class CanaryObservation(NamedTuple):
    leaf: str
    # Only a confirmed 404 produces None. Existing values never leave parsing.
    sha256: str | None


class PreflightOutcome(NamedTuple):
    initialize_config: bool
    canaries: tuple[CanaryObservation, ...]


def _fields(value: object) -> dict[str, object]:
    """Normalize JSON objects only at the file/HTTP transport boundary."""
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ApplyError("malformed OpenBao configuration or response object")
    return dict(value)


def _string(fields: dict[str, object], key: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str):
        raise ApplyError(f"missing or malformed OpenBao field: {key}")
    return value


def _config(value: object) -> JwtConfig:
    fields = _fields(value)
    return JwtConfig(
        _string(fields, "oidc_discovery_url"),
        _string(fields, "bound_issuer"),
        _string(fields, "default_role"),
    )


def planned_writes(doc: object) -> ApplyPlan:
    fields = _fields(doc)
    mount = _string(fields, "auth_mount")
    config = _config(fields)
    writes = [PlannedWrite("PUT", f"/v1/auth/{mount}/config", dict(config.body()))]
    for name, role in _fields(fields.get("roles")).items():
        policy = (HERE / "policies" / f"{name}.hcl").read_text(encoding="utf-8")
        writes.append(
            PlannedWrite("PUT", f"/v1/sys/policies/acl/{name}", {"policy": policy})
        )
        writes.append(
            PlannedWrite("PUT", f"/v1/auth/{mount}/role/{name}", _fields(role))
        )
    return ApplyPlan(mount, config, tuple(writes))


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep administrative headers on the explicitly selected endpoint only."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        return None


def _request(
    addr: str,
    token: str,
    method: Literal["GET", "PUT", "POST"],
    path: str,
    body: dict[str, object] | None,
) -> HttpResponse:
    req = urllib.request.Request(  # noqa: S310 - addr is http(s)-only via validated_openbao_address.
        f"{addr}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Vault-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.build_opener(NoRedirectHandler()).open(
            req, timeout=10
        ) as resp:  # nosec B310  # noqa: S310 - http(s)-only addr, validated above.
            raw = resp.read()
            return HttpResponse(resp.status, json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, {})
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ApplyError(f"OpenBao request failed: {method} {path}") from exc


def verify_kv2_mount(addr: str, token: str) -> None:
    """D4: policies name secret/data/... leaves; fail loudly if not KV-v2."""
    response = _request(addr, token, "GET", "/v1/sys/internal/ui/mounts/secret", None)
    if response.status != 200:
        raise ApplyError("secret/ KV-v2 mount check failed; refusing all writes")
    data = _fields(_fields(response.payload).get("data"))
    if (
        data.get("type") != "kv"
        or str(_fields(data.get("options")).get("version")) != "2"
    ):
        raise ApplyError("secret/ is not a KV-v2 mount; refusing all writes")


def preflight(addr: str, token: str, plan: ApplyPlan) -> PreflightOutcome:
    """Read every prerequisite before any mutation; ambiguous state refuses."""
    verify_kv2_mount(addr, token)
    response = _request(addr, token, "GET", "/v1/sys/auth", None)
    if response.status != 200:
        raise ApplyError("JWT auth mount check failed; refusing all writes")
    mounts = _fields(_fields(response.payload).get("data"))
    mount = _fields(mounts.get(plan.auth_mount.rstrip("/") + "/"))
    if mount.get("type") != "jwt":
        raise ApplyError("expected JWT auth mount is missing or has wrong type")
    response = _request(addr, token, "GET", f"/v1/auth/{plan.auth_mount}/config", None)
    initialize_config = response.status == 404
    if response.status == 200:
        live = _config(_fields(response.payload).get("data"))
        if live != plan.config:
            raise ApplyError("shared JWT configuration drift; refusing all writes")
    elif not initialize_config:
        raise ApplyError(f"JWT config read failed (status {response.status})")

    responses = tuple(
        (leaf, _request(addr, token, "GET", f"/v1/{leaf}", None)) for leaf in CANARIES
    )
    canaries = []
    for leaf, response in responses:
        digest = None
        if response.status == 200:
            data = _fields(_fields(_fields(response.payload).get("data")).get("data"))
            value = _string(data, "value")
            if not value:
                raise ApplyError(f"empty canary at {leaf}; refusing all writes")
            digest = hashlib.sha256(value.encode()).hexdigest()
        elif response.status != 404:
            raise ApplyError(
                f"canary read failed for {leaf} (status {response.status})"
            )
        canaries.append(CanaryObservation(leaf, digest))
    return PreflightOutcome(initialize_config, tuple(canaries))


def seed_canaries(
    addr: str, token: str, observations: tuple[CanaryObservation, ...]
) -> None:
    for observation in observations:
        leaf = observation.leaf
        if observation.sha256 is not None:
            print(f"canary present (unchanged) {leaf} sha256:{observation.sha256}")
            continue
        value = secrets.token_hex(32)
        response = _request(
            addr,
            token,
            "POST",
            f"/v1/{leaf}",
            {"options": {"cas": 0}, "data": {"value": value}},
        )
        if response.status not in (200, 204):
            raise ApplyError(
                f"canary create-only seed failed for {leaf} (status {response.status}); "
                "no overwrite or retry attempted"
            )
        print(
            f"canary seeded {leaf} sha256:{hashlib.sha256(value.encode()).hexdigest()}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--addr", default=os.environ.get("OPENBAO_ADDR", "http://127.0.0.1:8200")
    )
    args = parser.parse_args(argv)
    print("=" * 70)
    print("HUMAN-RUN ONLY: applies OpenBao JWT config for dotmac_sub Increment 1.")
    print("Review the output. Never run from CI or an agent session.")
    print("=" * 70)
    try:
        addr = validated_openbao_address(args.addr)
        plan = planned_writes(
            json.loads((HERE / "roles.json").read_text(encoding="utf-8"))
        )
        if args.dry_run:
            print(f"DRY-RUN target: {addr} (no contact made)")
            print("CHECK GET /v1/sys/internal/ui/mounts/secret (require kv version=2)")
            print("CHECK GET /v1/sys/auth (require existing jwt auth mount)")
            print(f"CHECK GET /v1/auth/{plan.auth_mount}/config (absent or compatible)")
            for leaf in CANARIES:
                print(f"CHECK GET /v1/{leaf} (404 absent or valid 200 only)")
            for index, (method, path, body) in enumerate(plan.writes):
                action = "INITIALIZE-IF-ABSENT" if index == 0 else "WRITE"
                print(f"{action} {method} {path} {json.dumps(body, sort_keys=True)}")
            for leaf in CANARIES:
                print(
                    f'SEED-IF-ABSENT POST /v1/{leaf} {{"options":{{"cas":0}},"data":{{"value":"<generated, never printed>"}}}}'
                )
            return 0
        token = os.environ.get("OPENBAO_TOKEN", "")
        if not token:
            raise ApplyError("OPENBAO_TOKEN is required for a real apply")
        outcome = preflight(addr, token, plan)
        print("Read-only preflight passed; compatible shared JWT config is preserved.")
        writes = plan.writes if outcome.initialize_config else plan.writes[1:]
        for method, path, body in writes:
            status, _ = _request(addr, token, method, path, body)
            if status not in (200, 204):
                raise ApplyError(f"write failed: {method} {path} (status {status})")
            print(f"applied {method} {path}")
        seed_canaries(addr, token, outcome.canaries)
        print("apply complete")
        return 0
    except ApplyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
