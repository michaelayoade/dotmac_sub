"""Subprocess worker for the OLT SSH limiter multi-process test.

Not a test module. Run as ``python olt_limiter_worker.py <json-args>``; it
takes one slot against the PostgreSQL named in the args, appends JSON lines to
a shared log (single ``O_APPEND`` write each, so lines never interleave), holds
the slot, then releases it.

Logging order is deliberately conservative for a concurrency proof: ``acquired``
is logged AFTER the lock is held and ``released`` BEFORE it is dropped, so the
overlap measured from the log can never exceed the true overlap.
"""

from __future__ import annotations

import json
import os
import sys
import time


def _log(path: str, **event: object) -> None:
    event["pid"] = os.getpid()
    event["t_ns"] = time.time_ns()
    line = (json.dumps(event) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def main() -> int:
    args = json.loads(sys.argv[1])
    from app.services.network.olt_ssh_limiter import (
        OltSessionBusy,
        OltSshPriority,
        acquire_slot,
    )

    label = args.get("label", "worker")
    # Release all contenders at the same wall-clock instant so slow imports in
    # one process cannot serialise the race the test is meant to create.
    start_at_ns = args.get("start_at_ns")
    if start_at_ns:
        delay = (int(start_at_ns) - time.time_ns()) / 1e9
        if delay > 0:
            time.sleep(delay)
    try:
        lease = acquire_slot(
            olt_key=args["olt_key"],
            limit=int(args["limit"]),
            reserved=int(args["reserved"]),
            priority=OltSshPriority[args["priority"]],
            wait_seconds=float(args["wait_seconds"]),
            database_url=args["database_url"],
        )
    except OltSessionBusy as exc:
        _log(args["log"], event="busy", label=label, message=str(exc))
        return 0
    _log(args["log"], event="acquired", label=label, slot=lease.slot_index)
    try:
        time.sleep(float(args["hold_seconds"]))
    finally:
        _log(args["log"], event="released", label=label)
        lease.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
