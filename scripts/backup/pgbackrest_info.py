#!/usr/bin/env python3
"""Validate pgBackRest JSON info and report the newest completed backup."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Any


class BackupHealthError(ValueError):
    """Raised when repository metadata cannot prove a fresh usable backup."""


@dataclass(frozen=True)
class BackupHealth:
    stanza: str
    label: str
    backup_type: str
    completed_at: int
    age_seconds: int


def evaluate_info(
    payload: Any,
    *,
    stanza: str,
    max_age_seconds: int,
    now: int | None = None,
) -> BackupHealth:
    if not isinstance(payload, list):
        raise BackupHealthError("pgBackRest info must be a list")
    stanza_info = next(
        (
            item
            for item in payload
            if isinstance(item, dict) and item.get("name") == stanza
        ),
        None,
    )
    if stanza_info is None:
        raise BackupHealthError(f"pgBackRest stanza not found: {stanza}")

    status = stanza_info.get("status")
    if not isinstance(status, dict) or int(status.get("code", -1)) != 0:
        message = (
            status.get("message") if isinstance(status, dict) else "missing status"
        )
        raise BackupHealthError(f"pgBackRest stanza is unhealthy: {message}")
    for repo in stanza_info.get("repo") or []:
        repo_status = repo.get("status") if isinstance(repo, dict) else None
        if not isinstance(repo_status, dict) or int(repo_status.get("code", -1)) != 0:
            message = (
                repo_status.get("message")
                if isinstance(repo_status, dict)
                else "missing status"
            )
            raise BackupHealthError(f"pgBackRest repository is unhealthy: {message}")

    completed: list[tuple[int, dict[str, Any]]] = []
    for backup in stanza_info.get("backup") or []:
        if not isinstance(backup, dict) or backup.get("error") is True:
            continue
        timestamp = backup.get("timestamp")
        if not isinstance(timestamp, dict) or timestamp.get("stop") is None:
            continue
        try:
            completed.append((int(timestamp["stop"]), backup))
        except (TypeError, ValueError):
            continue
    if not completed:
        raise BackupHealthError("pgBackRest has no completed backup")

    completed_at, latest = max(completed, key=lambda item: item[0])
    current_time = int(time.time() if now is None else now)
    age_seconds = max(0, current_time - completed_at)
    if age_seconds > max_age_seconds:
        raise BackupHealthError(
            f"latest pgBackRest backup is stale: age={age_seconds}s max={max_age_seconds}s"
        )
    label = str(latest.get("label") or "").strip()
    backup_type = str(latest.get("type") or "").strip()
    if not label or backup_type not in {"full", "diff", "incr"}:
        raise BackupHealthError("latest pgBackRest backup metadata is incomplete")
    return BackupHealth(
        stanza=stanza,
        label=label,
        backup_type=backup_type,
        completed_at=completed_at,
        age_seconds=age_seconds,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stanza", default="dotmac-sub")
    parser.add_argument("--max-age-seconds", type=int, default=36_000)
    parser.add_argument("--now", type=int)
    parser.add_argument("--format", choices=("human", "tsv"), default="human")
    args = parser.parse_args()
    try:
        health = evaluate_info(
            json.load(sys.stdin),
            stanza=args.stanza,
            max_age_seconds=max(1, args.max_age_seconds),
            now=args.now,
        )
    except (BackupHealthError, json.JSONDecodeError) as exc:
        print(f"BACKUP HEALTH FAILURE: {exc}", file=sys.stderr)
        return 2
    if args.format == "tsv":
        print(
            "\t".join(
                (
                    health.label,
                    health.backup_type,
                    str(health.completed_at),
                    str(health.age_seconds),
                )
            )
        )
    else:
        print(
            f"backup_ok stanza={health.stanza} label={health.label} "
            f"type={health.backup_type} age_seconds={health.age_seconds}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
