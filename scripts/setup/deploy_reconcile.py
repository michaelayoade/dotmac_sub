#!/usr/bin/env python
"""Read-only deployment reconciliation checks for production-like stacks."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

logging.basicConfig(level=logging.CRITICAL)

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import select

from app.db import SessionLocal, get_engine
from app.models.domain_settings import SettingDomain
from app.models.tr069 import Tr069AcsServer
from app.services.genieacs_client import GenieACSError, create_genieacs_client
from app.services.scheduler_config import find_unregistered_scheduled_tasks
from app.services.secrets import list_secret_paths, read_secret_fields
from app.services.settings_spec import resolve_value

REQUIRED_OPENBAO_PATHS = [
    "auth",
    "zabbix",
]

OPTIONAL_OPENBAO_PATHS = [
    "database",
    "redis",
    "radius",
    "genieacs",
    "s3",
    "migration",
]

ACS_ENFORCEMENT_NAME_PREFIX = "dotmac-enforce-acs"
ACS_CHECK_TIMEOUT_SECONDS = float(
    os.getenv("DEPLOY_RECONCILE_ACS_TIMEOUT_SECONDS", "5")
)


@dataclass
class CheckResult:
    name: str
    ok: bool
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


def _release_metadata() -> dict[str, str | None]:
    return {
        "app_env": os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or "unknown",
        "app_release": os.getenv("APP_RELEASE") or os.getenv("IMAGE_TAG") or None,
        "git_sha": os.getenv("GIT_SHA") or os.getenv("COMMIT_SHA") or None,
    }


def check_release() -> CheckResult:
    metadata = _release_metadata()
    ok = bool(metadata["app_release"] or metadata["git_sha"])
    summary = (
        "release metadata present"
        if ok
        else "release metadata missing; set APP_RELEASE or GIT_SHA in runtime env"
    )
    return CheckResult("release", ok, summary, metadata)


def check_migrations() -> CheckResult:
    alembic_cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(alembic_cfg)
    expected_heads = sorted(script.get_heads())

    engine = get_engine()
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        current_heads = sorted(context.get_current_heads())

    ok = current_heads == expected_heads
    summary = (
        "database schema is at expected head"
        if ok
        else "database schema drift detected"
    )
    return CheckResult(
        "migrations",
        ok,
        summary,
        {
            "current_heads": current_heads,
            "expected_heads": expected_heads,
        },
    )


def check_scheduler() -> CheckResult:
    from app.celery_app import celery_app

    drift = find_unregistered_scheduled_tasks(celery_app.tasks.keys())
    ok = not drift
    summary = (
        "enabled scheduled tasks are registered"
        if ok
        else "enabled scheduled tasks reference code that is not registered"
    )
    return CheckResult(
        "scheduler",
        ok,
        summary,
        {"unknown_task_count": len(drift), "unknown_tasks": drift},
    )


def check_openbao() -> CheckResult:
    optional_paths = list(OPTIONAL_OPENBAO_PATHS)
    db = SessionLocal()
    try:
        paystack_configured = any(
            [
                os.getenv("PAYSTACK_SECRET_KEY"),
                os.getenv("PAYSTACK_PUBLIC_KEY"),
                resolve_value(db, SettingDomain.billing, "paystack_secret_key"),
                resolve_value(db, SettingDomain.billing, "paystack_public_key"),
                resolve_value(db, SettingDomain.billing, "payment_gateway_provider")
                == "paystack",
            ]
        )
        notifications_configured = any(
            [
                os.getenv("SMTP_HOST"),
                os.getenv("SMTP_PORT"),
                os.getenv("SMTP_USERNAME"),
                os.getenv("SMTP_PASSWORD"),
                os.getenv("SMS_API_KEY"),
                os.getenv("SMS_API_SECRET"),
                resolve_value(db, SettingDomain.notification, "smtp_host"),
                resolve_value(db, SettingDomain.notification, "smtp_port"),
                resolve_value(db, SettingDomain.notification, "smtp_username"),
                resolve_value(db, SettingDomain.notification, "smtp_password"),
            ]
        )
    finally:
        db.close()

    if paystack_configured:
        optional_paths.append("paystack")
    if notifications_configured:
        optional_paths.append("notifications")

    expected_paths = REQUIRED_OPENBAO_PATHS + optional_paths
    paths = set(list_secret_paths())
    missing_required_paths = [
        path for path in REQUIRED_OPENBAO_PATHS if path not in paths
    ]
    missing_optional_paths = [path for path in optional_paths if path not in paths]
    field_presence: dict[str, list[str]] = {}
    for path in sorted(paths.intersection(expected_paths)):
        field_presence[path] = sorted(read_secret_fields(path).keys())

    ok = not missing_required_paths
    summary = (
        "required OpenBao secret paths present"
        if ok
        else "required OpenBao secret paths missing"
    )
    return CheckResult(
        "openbao",
        ok,
        summary,
        {
            "available_paths": sorted(paths),
            "required_paths": REQUIRED_OPENBAO_PATHS,
            "optional_paths": optional_paths,
            "missing_required_paths": missing_required_paths,
            "missing_optional_paths": missing_optional_paths,
            "field_presence": field_presence,
        },
    )


def _acs_enforcement_status(server: Tr069AcsServer) -> dict[str, Any]:
    if not server.base_url:
        return {
            "exists": False,
            "error": "ACS server has no GenieACS base URL configured",
        }

    server_slug = str(server.id).replace("-", "")[:12]
    provision_name = f"{ACS_ENFORCEMENT_NAME_PREFIX}-{server_slug}"
    preset_id = f"{ACS_ENFORCEMENT_NAME_PREFIX}-{server_slug}"
    client = create_genieacs_client(
        server.base_url,
        timeout=ACS_CHECK_TIMEOUT_SECONDS,
    )

    status: dict[str, Any] = {
        "provision_id": provision_name,
        "preset_id": preset_id,
        "provision_exists": False,
        "preset_exists": False,
        "preset_details": None,
        "errors": [],
    }

    try:
        provisions = client.list_provisions()
        status["provision_exists"] = any(
            provision.get("_id") == provision_name for provision in provisions
        )
    except GenieACSError as exc:
        status["errors"].append(f"provisions: {exc}")

    try:
        presets = client.list_presets()
        for preset in presets:
            if preset.get("_id") == preset_id:
                status["preset_exists"] = True
                status["preset_details"] = {
                    "events": preset.get("events", {}),
                    "precondition": preset.get("precondition", ""),
                    "weight": preset.get("weight", 0),
                }
                break
    except GenieACSError as exc:
        status["errors"].append(f"presets: {exc}")

    status["exists"] = status["provision_exists"] and status["preset_exists"]
    return status


def check_acs_runtime() -> CheckResult:
    db = SessionLocal()
    try:
        servers = list(
            db.scalars(
                select(Tr069AcsServer)
                .where(Tr069AcsServer.is_active.is_(True))
                .order_by(Tr069AcsServer.name.asc())
            ).all()
        )
        server_results: list[dict[str, Any]] = []
        all_ok = True
        for server in servers:
            enforcement = _acs_enforcement_status(server)
            server_ok = bool(enforcement.get("exists"))
            all_ok = all_ok and server_ok
            server_results.append(
                {
                    "id": str(server.id),
                    "name": server.name,
                    "base_url": server.base_url,
                    "cwmp_url": server.cwmp_url,
                    "periodic_inform_interval": server.periodic_inform_interval,
                    "acs_enforcement": enforcement,
                    "ok": server_ok,
                }
            )
    finally:
        db.close()

    ok = bool(servers) and all_ok
    if not servers:
        summary = "no active ACS servers configured"
    elif ok:
        summary = "ACS enforcement artifacts present for all active servers"
    else:
        summary = "ACS enforcement artifacts missing or incomplete"
    return CheckResult(
        "acs_runtime",
        ok,
        summary,
        {"active_server_count": len(servers), "servers": server_results},
    )


def _run_check(name: str, fn) -> CheckResult:
    try:
        return fn()
    except Exception as exc:
        return CheckResult(
            name,
            False,
            f"{name} check failed",
            {
                "error": str(exc),
                "exception_type": type(exc).__name__,
            },
        )


def run_checks() -> list[CheckResult]:
    return [
        _run_check("release", check_release),
        _run_check("migrations", check_migrations),
        _run_check("scheduler", check_scheduler),
        _run_check("openbao", check_openbao),
        _run_check("acs_runtime", check_acs_runtime),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of plain text.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    results = run_checks()
    ok = all(result.ok for result in results)

    if args.json:
        payload = {
            "ok": ok,
            "checks": [asdict(result) for result in results],
        }
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0 if ok else 1

    print("Deployment reconciliation")
    for result in results:
        status = "OK" if result.ok else "FAIL"
        print(f"[{status}] {result.name}: {result.summary}")
        for key, value in result.details.items():
            if value in (None, "", [], {}):
                continue
            rendered = (
                json.dumps(value, sort_keys=True, default=str)
                if isinstance(value, (dict, list))
                else str(value)
            )
            print(f"  {key}: {rendered}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
