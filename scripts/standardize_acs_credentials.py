"""Standardize GenieACS device ManagementServer credentials in controlled batches."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.db import SessionLocal
from app.models.tr069 import Tr069AcsServer
from app.services.credential_crypto import decrypt_credential
from app.services.genieacs_client import GenieACSError, create_genieacs_client


def _value(device: dict[str, Any], path: str) -> Any:
    current: Any = device
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    if isinstance(current, dict):
        if "_value" in current:
            return current["_value"]
        return None
    return current


def _has_object(device: dict[str, Any], path: str) -> bool:
    current: Any = device
    for part in path.split("."):
        if not isinstance(current, dict):
            return False
        current = current.get(part)
        if current is None:
            return False
    return isinstance(current, dict)


def _root(device: dict[str, Any]) -> str | None:
    if _has_object(device, "Device.ManagementServer") or any(
        _value(device, f"Device.ManagementServer.{leaf}") is not None
        for leaf in ("Username", "ConnectionRequestUsername")
    ):
        return "Device"
    if _has_object(device, "InternetGatewayDevice.ManagementServer") or any(
        _value(device, f"InternetGatewayDevice.ManagementServer.{leaf}") is not None
        for leaf in ("Username", "ConnectionRequestUsername")
    ):
        return "InternetGatewayDevice"
    return None


def _last_inform(device: dict[str, Any]) -> datetime | None:
    value = device.get("_lastInform")
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(UTC)
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return None


def _active_server() -> Tr069AcsServer:
    with SessionLocal() as db:
        server = db.scalars(
            select(Tr069AcsServer)
            .where(Tr069AcsServer.is_active.is_(True))
            .order_by(Tr069AcsServer.name.asc())
            .limit(1)
        ).first()
        if server is None:
            raise SystemExit("No active TR-069 ACS server is configured.")
        db.expunge(server)
        return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--recent-hours", type=int, default=24)
    parser.add_argument("--include-missing", action="store_true")
    parser.add_argument("--include-stale", action="store_true")
    args = parser.parse_args()

    server = _active_server()
    cwmp_username = str(server.cwmp_username or "").strip()
    cwmp_password = decrypt_credential(server.cwmp_password) if server.cwmp_password else ""
    cr_username = str(server.connection_request_username or "").strip()
    cr_password = (
        decrypt_credential(server.connection_request_password)
        if server.connection_request_password
        else ""
    )
    if not all([cwmp_username, cwmp_password, cr_username, cr_password]):
        raise SystemExit("Active ACS server is missing one or more credentials.")

    client = create_genieacs_client(server.base_url, timeout=20)
    # GenieACS NBI projections on dotted parameter paths vary by version; for
    # this fleet-sized operation the full snapshots are more reliable.
    devices = client.list_devices()
    cutoff = datetime.now(UTC) - timedelta(hours=args.recent_hours)

    counts: Counter[str] = Counter()
    candidates: list[tuple[dict[str, Any], str, str, str | None]] = []
    for device in devices:
        root = _root(device)
        if root is None:
            counts["no_root"] += 1
            continue
        cwmp = _value(device, f"{root}.ManagementServer.Username")
        cr = _value(device, f"{root}.ManagementServer.ConnectionRequestUsername")
        if cwmp == cwmp_username:
            counts["cwmp_ok"] += 1
        elif cwmp is None:
            counts["cwmp_missing"] += 1
        else:
            counts["cwmp_other"] += 1
        if cr == cr_username:
            counts["cr_ok"] += 1
        elif cr is None:
            counts["cr_missing"] += 1
        else:
            counts["cr_other"] += 1

        needs_cwmp = cwmp != cwmp_username and (cwmp is not None or args.include_missing)
        needs_cr = cr != cr_username and (cr is not None or args.include_missing)
        if not needs_cwmp and not needs_cr:
            continue

        last = _last_inform(device)
        if not args.include_stale and (last is None or last < cutoff):
            counts["candidate_stale_skipped"] += 1
            continue
        candidates.append((device, root, str(cwmp), str(cr)))

    print(
        "snapshot:",
        dict(counts),
        "total_devices=",
        len(devices),
        "eligible_candidates=",
        len(candidates),
    )
    selected = candidates[: max(args.limit, 0)]
    for device, root, cwmp, cr in selected:
        print(
            "candidate:",
            device.get("_id"),
            "root=",
            root,
            "cwmp=",
            cwmp,
            "cr=",
            cr,
            "lastInform=",
            _last_inform(device),
        )

    if not args.apply:
        print("dry-run: no tasks submitted")
        return

    submitted = 0
    failed = 0
    for device, root, _cwmp, _cr in selected:
        device_id = str(device["_id"])
        params = {
            f"{root}.ManagementServer.Username": cwmp_username,
            f"{root}.ManagementServer.Password": cwmp_password,
            f"{root}.ManagementServer.ConnectionRequestUsername": cr_username,
            f"{root}.ManagementServer.ConnectionRequestPassword": cr_password,
            f"{root}.ManagementServer.PeriodicInformEnable": True,
            f"{root}.ManagementServer.PeriodicInformInterval": int(
                server.periodic_inform_interval or 300
            ),
        }
        try:
            result = client.set_parameter_values(device_id, params)
            submitted += 1
            print("submitted:", device_id, "task=", result.get("_id"))
        except GenieACSError as exc:
            failed += 1
            print("failed:", device_id, exc)
    print("result:", {"submitted": submitted, "failed": failed})


if __name__ == "__main__":
    main()
