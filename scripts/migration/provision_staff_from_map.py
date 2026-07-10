#!/usr/bin/env python3
"""Phase 1 staff provisioning: create sub system_user accounts for CRM staff.

Input is ``unmatched_active.csv`` from ``build_crm_staff_map.py`` (active CRM
staff with no matching sub ``system_users`` row). For each row this script
creates a system user through the same service path the admin UI uses
(``app.services.web_system_user_mutations.create_user_with_role_and_password``):
SystemUser + SystemUserRole + local UserCredential with a random, discarded
temporary password and ``must_change_password=True``. No password ever appears
in any CSV or output; staff get access via the password-reset/invite flow
(``--send-invites`` or the admin UI's bulk "send invite" action).

Inputs:
  SUB_DATABASE_URL=postgresql://...   (also seeds DATABASE_URL for app imports)

Dry-run by default; pass ``--apply`` to write. ``--role`` must name an
existing active RBAC role in sub (available roles are listed on mismatch).
``--skip`` accepts a CSV of ``crm_person_id`` values to exclude.

Outputs (``--out``, default ``staff-provision/``):
  * ``created.csv``   rows created (or that would be created in dry-run)
  * ``skipped.csv``   rows not created, with action + reason
  * ``staff_map_extension.csv``  ``crm_person_id -> system_user_id`` rows in
    the exact ``staff_map.csv`` column layout, so the ticket importer's
    ``--staff-map`` can consume the union of both files
  * ``summary.json``

Idempotent: re-running reports previously created users as
``already_exists`` and still emits their mapping rows.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_COLUMNS = {
    "crm_person_id",
    "crm_name",
    "crm_email",
    "credential_usernames",
    "person_is_active",
    "credential_is_active",
}

TRUE_VALUES = {"true", "t", "1", "yes", "y"}

ACTION_CREATE = "create"
ACTION_ALREADY_EXISTS = "already_exists"
ACTION_SKIPPED = "skipped"
ACTION_INACTIVE = "inactive"
ACTION_NO_EMAIL = "no_email"
ACTION_DUPLICATE_EMAIL = "duplicate_email_in_csv"
ACTION_ERROR = "error"

# staff_map.csv layout from build_crm_staff_map.py — the ticket importer's
# --staff-map loader reads crm_person_id + system_user_id from it.
STAFF_MAP_FIELDNAMES = [
    "crm_person_id",
    "crm_name",
    "crm_email",
    "system_user_id",
    "match_via",
]

MATCH_VIA_PROVISIONED = "provisioned"
MATCH_VIA_PROVISIONED_EXISTING = "provisioned_existing"


def parse_bool(value: Any) -> bool:
    """Parse the CSV's stringified booleans (``True``/``False``)."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in TRUE_VALUES


def normalize_email(value: str | None) -> str | None:
    """Lower/strip an email; None for empty or non-email strings."""
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized or "@" not in normalized:
        return None
    return normalized


def candidate_email(row: dict[str, Any]) -> str | None:
    """Best usable email for a staff row: person email, then credentials.

    Mirrors ``build_crm_staff_map.candidate_emails`` ordering (person email
    first, then ``;``-joined credential usernames that look like emails).
    """
    email = normalize_email(row.get("crm_email"))
    if email:
        return email
    for username in str(row.get("credential_usernames") or "").split(";"):
        email = normalize_email(username)
        if email:
            return email
    return None


def split_name(name: str) -> tuple[str, str]:
    """Split a CRM display name into (first_name, last_name) for SystemUser.

    First whitespace token becomes first_name, the remainder last_name (may be
    empty — the column is non-null but accepts an empty string). Both are
    truncated to the model's String(80).
    """
    parts = str(name or "").strip().split()
    if not parts:
        return ("", "")
    first = parts[0][:80]
    last = " ".join(parts[1:])[:80]
    return (first, last)


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    email: str | None = None
    system_user_id: str | None = None


def decide_row(
    row: dict[str, Any],
    existing_by_email: dict[str, str],
    skip_ids: set[str],
) -> Decision:
    """Pure per-row decision: create, already_exists, or a skip class."""
    crm_person_id = str(row.get("crm_person_id") or "").strip().lower()
    if crm_person_id in skip_ids:
        return Decision(ACTION_SKIPPED, "in_skip_list")
    if not (
        parse_bool(row.get("person_is_active"))
        and parse_bool(row.get("credential_is_active"))
    ):
        return Decision(ACTION_INACTIVE, "crm_staff_not_active")
    email = candidate_email(row)
    if not email:
        return Decision(ACTION_NO_EMAIL, "no_usable_email")
    existing_id = existing_by_email.get(email)
    if existing_id:
        return Decision(
            ACTION_ALREADY_EXISTS,
            "system_user_email_exists",
            email=email,
            system_user_id=existing_id,
        )
    return Decision(ACTION_CREATE, "unmatched_active_crm_staff", email=email)


def plan_rows(
    rows: list[dict[str, Any]],
    existing_by_email: dict[str, str],
    skip_ids: set[str],
) -> list[tuple[dict[str, Any], Decision]]:
    """Decide every row, downgrading in-CSV email collisions.

    Two CRM staff rows resolving to the same email would violate the unique
    system_users.email — only the first keeps ``create``; later ones are
    reported as ``duplicate_email_in_csv`` for manual triage.
    """
    planned: list[tuple[dict[str, Any], Decision]] = []
    claimed_emails: set[str] = set()
    for row in rows:
        decision = decide_row(row, existing_by_email, skip_ids)
        email = decision.email
        if decision.action == ACTION_CREATE and email:
            if email in claimed_emails:
                decision = Decision(
                    ACTION_DUPLICATE_EMAIL,
                    "email_claimed_by_earlier_csv_row",
                    email=email,
                )
            else:
                claimed_emails.add(email)
        planned.append((row, decision))
    return planned


def load_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - fieldnames
        if missing:
            raise SystemExit(
                f"{path}: missing required columns: {', '.join(sorted(missing))} "
                "(expected build_crm_staff_map.py unmatched_active.csv)"
            )
        return list(reader)


def load_skip_ids(path: Path | None) -> set[str]:
    """Load crm_person_ids to exclude from a CSV with a crm_person_id column."""
    if path is None:
        return set()
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if "crm_person_id" not in (reader.fieldnames or []):
            raise SystemExit(f"{path}: skip CSV needs a crm_person_id column")
        return {
            value
            for row in reader
            if (value := str(row.get("crm_person_id") or "").strip().lower())
        }


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _staff_map_row(
    row: dict[str, Any], system_user_id: str, match_via: str
) -> dict[str, Any]:
    return {
        "crm_person_id": str(row.get("crm_person_id") or "").strip(),
        "crm_name": row.get("crm_name") or "",
        "crm_email": candidate_email(row) or "",
        "system_user_id": system_user_id,
        "match_via": match_via,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--csv",
        required=True,
        help="unmatched_active.csv from build_crm_staff_map.py",
    )
    parser.add_argument(
        "--role",
        required=True,
        help="Existing active sub RBAC role name to assign (e.g. 'Technical support')",
    )
    parser.add_argument(
        "--skip",
        help="Optional CSV (crm_person_id column) of staff to exclude",
    )
    parser.add_argument(
        "--out",
        default="staff-provision",
        help="Directory for created/skipped/staff_map_extension CSVs and summary.json",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write to the database (default is dry-run)",
    )
    parser.add_argument(
        "--send-invites",
        action="store_true",
        help="With --apply: email each created user a password-reset invite",
    )
    args = parser.parse_args()

    sub_url = os.environ.get("SUB_DATABASE_URL")
    if not sub_url:
        raise SystemExit("SUB_DATABASE_URL is required")
    # App modules read settings.database_url at import time; point them at the
    # same database so no separate .env is required in a script context.
    os.environ.setdefault("DATABASE_URL", sub_url)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    # Imported lazily so the pure decision logic above stays importable in
    # tests without app/database context.
    from sqlalchemy import create_engine, func
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.orm import Session

    from app.models.rbac import Role
    from app.models.system_user import SystemUser
    from app.services import web_system_user_mutations as mutations

    rows = load_rows(Path(args.csv))
    skip_ids = load_skip_ids(Path(args.skip) if args.skip else None)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = create_engine(sub_url)
    created_rows: list[dict[str, Any]] = []
    skipped_rows: list[dict[str, Any]] = []
    staff_map_rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    invites_sent = 0

    with Session(engine) as db:
        role = (
            db.query(Role)
            .filter(func.lower(Role.name) == args.role.strip().lower())
            .filter(Role.is_active.is_(True))
            .one_or_none()
        )
        if role is None:
            available = [
                name
                for (name,) in db.query(Role.name)
                .filter(Role.is_active.is_(True))
                .order_by(Role.name)
            ]
            raise SystemExit(
                f"Role {args.role!r} not found among active sub RBAC roles. "
                f"Available: {', '.join(available) or '(none)'}"
            )
        # Plain strings: the per-user service commits expire ORM instances.
        role_id = str(role.id)
        role_name = role.name

        existing_by_email: dict[str, str] = {}
        for user_email, user_id in db.query(SystemUser.email, SystemUser.id):
            email = normalize_email(user_email)
            if email:
                existing_by_email[email] = str(user_id)

        for row, decision in plan_rows(rows, existing_by_email, skip_ids):
            action = decision.action
            if action == ACTION_ALREADY_EXISTS and decision.system_user_id:
                staff_map_rows.append(
                    _staff_map_row(
                        row, decision.system_user_id, MATCH_VIA_PROVISIONED_EXISTING
                    )
                )
            if action == ACTION_CREATE:
                system_user_id = ""
                invite_note = ""
                if args.apply:
                    first_name, last_name = split_name(str(row.get("crm_name") or ""))
                    assert decision.email is not None
                    try:
                        # Same path as the admin UI user-create route: SystemUser
                        # + role link + local credential with a discarded random
                        # temp password and must_change_password=True.
                        system_user, _temp_password = (
                            mutations.create_user_with_role_and_password(
                                db,
                                first_name=first_name,
                                last_name=last_name,
                                email=decision.email,
                                role_id=role_id,
                            )
                        )
                    except SQLAlchemyError as exc:
                        db.rollback()
                        action = ACTION_ERROR
                        decision = Decision(
                            ACTION_ERROR,
                            f"create_failed: {exc.__class__.__name__}",
                            email=decision.email,
                        )
                    else:
                        system_user_id = str(system_user.id)
                        staff_map_rows.append(
                            _staff_map_row(row, system_user_id, MATCH_VIA_PROVISIONED)
                        )
                        if args.send_invites:
                            try:
                                invite_note = mutations.send_user_invite_for_user(
                                    db, user_id=system_user_id
                                )
                                invites_sent += 1
                            except Exception as exc:  # noqa: BLE001 — email is best-effort
                                db.rollback()
                                invite_note = f"invite_failed: {exc}"
                if action == ACTION_CREATE:
                    created_rows.append(
                        {
                            "crm_person_id": row.get("crm_person_id") or "",
                            "crm_name": row.get("crm_name") or "",
                            "email": decision.email or "",
                            "system_user_id": system_user_id,
                            "invite_note": invite_note,
                        }
                    )
            if action != ACTION_CREATE:
                skipped_rows.append(
                    {
                        "crm_person_id": row.get("crm_person_id") or "",
                        "crm_name": row.get("crm_name") or "",
                        "action": action,
                        "reason": decision.reason,
                        "email": decision.email or "",
                        "system_user_id": decision.system_user_id or "",
                    }
                )
            counts[action] = counts.get(action, 0) + 1

    write_csv(
        out_dir / "created.csv",
        created_rows,
        ["crm_person_id", "crm_name", "email", "system_user_id", "invite_note"],
    )
    write_csv(
        out_dir / "skipped.csv",
        skipped_rows,
        ["crm_person_id", "crm_name", "action", "reason", "email", "system_user_id"],
    )
    write_csv(out_dir / "staff_map_extension.csv", staff_map_rows, STAFF_MAP_FIELDNAMES)

    summary = {
        "dry_run": not args.apply,
        "csv": str(args.csv),
        "role": role_name,
        "role_id": role_id,
        "rows": len(rows),
        "counts": counts,
        "invites_sent": invites_sent,
        "out_dir": str(out_dir),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    if not args.apply:
        print("Dry run only. Re-run with --apply to create users.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
