#!/usr/bin/env python3
"""Apply Increment-1 OpenBao JWT roles/policies/canaries (human-run only).

Idempotent: policies/roles are PUT (converge); canaries seed only when
absent. Values are generated locally, never printed; only sha256 appears.
--dry-run contacts nothing and prints the exact writes.
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
from pathlib import Path
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


def planned_writes(doc: dict) -> list[tuple[str, str, dict]]:
    mount = doc["auth_mount"]
    writes = [
        (
            "PUT",
            f"/v1/auth/{mount}/config",
            {
                "oidc_discovery_url": doc["oidc_discovery_url"],
                "bound_issuer": doc["bound_issuer"],
                "default_role": doc["default_role"],
            },
        )
    ]
    for name, role in doc["roles"].items():
        policy = (HERE / "policies" / f"{name}.hcl").read_text(encoding="utf-8")
        writes.append(("PUT", f"/v1/sys/policies/acl/{name}", {"policy": policy}))
        writes.append(("PUT", f"/v1/auth/{mount}/role/{name}", role))
    return writes


def _request(addr, token, method, path, body):
    req = urllib.request.Request(  # noqa: S310 - addr is http(s)-only via validated_openbao_address.
        f"{addr}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Vault-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310  # noqa: S310 - http(s)-only addr, validated above.
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ApplyError(f"OpenBao request failed: {method} {path}") from exc


def verify_kv2_mount(addr, token):
    """D4: policies name secret/data/... leaves; fail loudly if not KV-v2."""
    status, payload = _request(
        addr, token, "GET", "/v1/sys/internal/ui/mounts/secret", None
    )
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    if (
        status != 200
        or data.get("type") != "kv"
        or str((data.get("options") or {}).get("version", "")) != "2"
    ):
        raise ApplyError(
            "secret/ is not a KV-v2 mount (D4 check failed); refusing to write policies"
        )


def seed_canaries(addr, token):
    for leaf in CANARIES:
        status, payload = _request(addr, token, "GET", f"/v1/{leaf}", None)
        existing = ((payload.get("data") or {}).get("data") or {}).get("value")
        if status == 200 and isinstance(existing, str) and existing:
            print(
                f"canary present (unchanged) {leaf} sha256:{hashlib.sha256(existing.encode()).hexdigest()}"
            )
            continue
        value = secrets.token_hex(32)
        status, _ = _request(
            addr, token, "POST", f"/v1/{leaf}", {"data": {"value": value}}
        )
        if status not in (200, 204):
            raise ApplyError(f"canary seed failed for {leaf} (status {status})")
        print(
            f"canary seeded {leaf} sha256:{hashlib.sha256(value.encode()).hexdigest()}"
        )


def main(argv=None) -> int:
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
        writes = planned_writes(
            json.loads((HERE / "roles.json").read_text(encoding="utf-8"))
        )
        if args.dry_run:
            print(f"DRY-RUN target: {addr} (no contact made)")
            print("CHECK GET /v1/sys/internal/ui/mounts/secret (require kv version=2)")
            for method, path, body in writes:
                print(f"WRITE {method} {path} {json.dumps(body, sort_keys=True)}")
            for leaf in CANARIES:
                print(
                    f'SEED-IF-ABSENT POST /v1/{leaf} {{"data":{{"value":"<generated, never printed>"}}}}'
                )
            return 0
        token = os.environ.get("OPENBAO_TOKEN", "")
        if not token:
            raise ApplyError("OPENBAO_TOKEN is required for a real apply")
        verify_kv2_mount(addr, token)
        print("KV-v2 mount at secret/ verified.")
        for method, path, body in writes:
            status, _ = _request(addr, token, method, path, body)
            if status not in (200, 204):
                raise ApplyError(f"write failed: {method} {path} (status {status})")
            print(f"applied {method} {path}")
        seed_canaries(addr, token)
        print("apply complete")
        return 0
    except ApplyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
