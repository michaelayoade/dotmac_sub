#!/usr/bin/env python3
"""Minimal example connector implementing the Dotmac runner wire contract.

Reads one RunnerRequest as JSON on stdin and writes one RunnerResponse as JSON
on stdout. It performs no real integration: it echoes a deterministic result
per verb so the transport mechanics (stdin/stdout, exit codes, deadline kill,
secret delivery) can be tested end to end without a provider.

Secrets arrive as environment variables under DM_SECRET_<NAME>. This connector
reports only the NAMES it received, never the values, so a delivery assertion
never has to observe a secret.

Test-only behaviour, driven by the request payload so tests can steer it:
- action "sleep_forever": block, to exercise the deadline kill.
- action "wrong_verb": answer health to a non-health request.
- action "crash": exit non-zero.
"""

import json
import os
import socket
import sys
import time

CONTRACT = "dotmac.io/integrations/runner/v1"


def _secret_names() -> list[str]:
    prefix = "DM_SECRET_"
    return sorted(
        key[len(prefix) :].lower() for key in os.environ if key.startswith(prefix)
    )


def _network_reachable() -> bool:
    """Whether the container can open an outbound connection at all.

    Used to prove default-deny egress: with --network=none this returns False.
    """
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=2):
            return True
    except OSError:
        return False


def main() -> int:
    raw = sys.stdin.read()
    request = json.loads(raw)
    verb = request.get("verb")
    payload = (request.get("envelope") or {}).get("payload") or {}
    action = payload.get("action")

    if action == "crash":
        sys.stderr.write("example connector deliberate crash\n")
        return 3
    if action == "sleep_forever":
        time.sleep(3600)
        return 0

    response: dict = {"contract_version": CONTRACT, "verb": verb}
    if action == "wrong_verb":
        response["verb"] = "health"
        response["health"] = {"status": "healthy", "details": {}}
    elif verb == "validate":
        response["validation"] = {
            "valid": True,
            "error_codes": [],
            "details": {"secrets_seen": _secret_names()},
        }
    elif verb == "execute":
        output = {"echo": payload, "secrets_seen": _secret_names()}
        if action == "probe_network":
            output["network_reachable"] = _network_reachable()
        response["operation"] = {
            "operation_id": request["envelope"]["operation_id"],
            "status": "succeeded",
            "output": output,
            "external_receipt": {},
            "error_code": None,
            "retry_after_seconds": None,
        }
    elif verb == "health":
        response["health"] = {"status": "healthy", "details": {}}
    elif verb == "cancel":
        response["canceled"] = True
    else:  # pragma: no cover - contract guarantees a known verb
        sys.stderr.write(f"unknown verb {verb!r}\n")
        return 2

    sys.stdout.write(json.dumps(response))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
