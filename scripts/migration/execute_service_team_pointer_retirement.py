"""Execute one separately approved legacy pointer-retirement plan."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from app.db import SessionLocal
from app.services.owner_commands import CommandContext
from app.services.service_team_pointer_retirement import (
    RetireLegacyServiceTeamPointers,
    ServiceTeamPointerRetirementApproval,
    ServiceTeamPointerRetirementError,
    ServiceTeamPointerRetirementPlan,
    retire_legacy_service_team_pointers,
)


def _read_json(path: Path) -> object:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print("REFUSED: --execute is required")
        return 2
    try:
        plan = ServiceTeamPointerRetirementPlan.from_payload(_read_json(args.plan))
        approval = ServiceTeamPointerRetirementApproval.from_payload(
            _read_json(args.approval)
        )
        command_id = uuid4()
        with SessionLocal() as db:
            outcome = retire_legacy_service_team_pointers(
                db,
                RetireLegacyServiceTeamPointers(
                    context=CommandContext(
                        command_id=command_id,
                        correlation_id=command_id,
                        actor=args.actor,
                        scope="service_team_pointer_retirement:execute",
                        reason=approval.reason,
                        idempotency_key=plan.plan_digest,
                    ),
                    plan=plan,
                    approval=approval,
                    plan_file_sha256=_sha256(args.plan),
                ),
            )
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        ServiceTeamPointerRetirementError,
    ) as exc:
        print(f"REFUSED: {exc}")
        return 2
    print(
        json.dumps(
            {
                "status": "applied",
                "plan_digest": outcome.plan_digest,
                "retired_pointer_count": outcome.retired_pointer_count,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
