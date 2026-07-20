#!/usr/bin/env python3
"""Phase 0 staff-map builder: crm_person_id -> sub system_user_id.

Inputs:
  SUB_DATABASE_URL=postgresql://...
  CRM_DATABASE_URL=postgresql://...

CRM ticket role/author fields hold CRM person UUIDs (staff). Sub needs a
durable ``crm_person_id -> system_user_id`` map so migrated tickets/comments
can be re-attributed and pickers can render names (unification spec
``10-phase1-tickets.md`` section 1.8).

Staff predicate (mirrors ``dotmac_crm`` ``agent_mentions.py``): a CRM person
is staff when they have at least one ``user_credentials`` row; they are
*active* staff when ``people.is_active`` and at least one credential has
``is_active``. Candidate emails per staff person: ``people.email`` plus any
credential ``username`` containing ``@`` (CRM login accepts an email as
username, ``auth_flow._resolve_local_credential``).

Matching: normalized (lower/strip) email equality against sub
``system_users.email``. Per staff person the candidate emails are tried in
order (person email first); the first email with exactly one sub match wins.
Classes: ``matched``, ``ambiguous`` (an email hits >1 sub user and no
candidate email hits exactly one), ``unmatched_active`` and
``unmatched_inactive`` (inactive CRM staff without a sub account are
expected — reported separately).

This script is a pure report/artifact builder: both database sessions are
forced READ ONLY and nothing is ever written to either database. Outputs go
to ``--out`` (default ``staff-map/``): ``staff_map.csv``, ``ambiguous.csv``,
``unmatched_active.csv``, ``unmatched_inactive.csv``,
``crm_people_directory.csv`` (the section 1.8 display-name directory for old
assignments) and ``summary.json``.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

MATCH_VIA_PERSON_EMAIL = "person_email"
MATCH_VIA_CREDENTIAL_USERNAME = "credential_username"

REPORT_NAMES = [
    "staff_map",
    "ambiguous",
    "unmatched_active",
    "unmatched_inactive",
    "crm_people_directory",
]


@dataclass(frozen=True)
class CrmStaffRow:
    person_id: str
    name: str
    person_email: str | None
    credential_usernames: tuple[str, ...]
    person_is_active: bool
    credential_is_active: bool

    @property
    def is_active(self) -> bool:
        return self.person_is_active and self.credential_is_active


@dataclass(frozen=True)
class SystemUserRow:
    id: str
    email: str | None
    name: str
    user_type: str | None
    is_active: bool


@dataclass
class StaffMapStats:
    crm_staff: int = 0
    crm_staff_active: int = 0
    sub_system_users: int = 0
    matched: int = 0
    matched_via_person_email: int = 0
    matched_via_credential_username: int = 0
    matched_inactive_sub_user: int = 0
    ambiguous: int = 0
    unmatched_active: int = 0
    unmatched_inactive: int = 0
    sub_users_matched_by_multiple_crm_staff: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "crm_staff": self.crm_staff,
            "crm_staff_active": self.crm_staff_active,
            "sub_system_users": self.sub_system_users,
            "matched": self.matched,
            "matched_via_person_email": self.matched_via_person_email,
            "matched_via_credential_username": self.matched_via_credential_username,
            "matched_inactive_sub_user": self.matched_inactive_sub_user,
            "ambiguous": self.ambiguous,
            "unmatched_active": self.unmatched_active,
            "unmatched_inactive": self.unmatched_inactive,
            "sub_users_matched_by_multiple_crm_staff": (
                self.sub_users_matched_by_multiple_crm_staff
            ),
        }


@dataclass
class StaffMapResult:
    reports: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {name: [] for name in REPORT_NAMES}
    )
    stats: StaffMapStats = field(default_factory=StaffMapStats)


def _engine_from_env(name: str) -> Engine:
    url = os.environ.get(name)
    if not url:
        raise SystemExit(f"{name} is required")
    return create_engine(url, pool_pre_ping=True)


def _rows(
    conn: Connection, sql: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    result = conn.execute(text(sql), params or {})
    return [dict(row._mapping) for row in result]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def normalize_email(value: str | None) -> str | None:
    """Lower/strip an email; None for empty or non-email strings."""
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized or "@" not in normalized:
        return None
    return normalized


def candidate_emails(staff: CrmStaffRow) -> list[tuple[str, str]]:
    """Ordered unique ``(normalized_email, match_via)`` candidates.

    ``people.email`` first, then credential usernames that look like emails.
    """
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    person_email = normalize_email(staff.person_email)
    if person_email:
        candidates.append((person_email, MATCH_VIA_PERSON_EMAIL))
        seen.add(person_email)
    for username in staff.credential_usernames:
        email = normalize_email(username)
        if email and email not in seen:
            candidates.append((email, MATCH_VIA_CREDENTIAL_USERNAME))
            seen.add(email)
    return candidates


def display_name(
    display: str | None, first: str | None, last: str | None, email: str | None
) -> str:
    """CRM picker label logic (``agent_mentions.py``): display, else names, else email."""
    label = (display or "").strip()
    if label:
        return label
    label = f"{(first or '').strip()} {(last or '').strip()}".strip()
    if label:
        return label
    return (email or "").strip() or "User"


def build_staff_map(
    crm_staff: list[CrmStaffRow], sub_users: list[SystemUserRow]
) -> StaffMapResult:
    result = StaffMapResult()
    stats = result.stats
    stats.crm_staff = len(crm_staff)
    stats.crm_staff_active = sum(1 for staff in crm_staff if staff.is_active)
    stats.sub_system_users = len(sub_users)

    by_email: dict[str, list[SystemUserRow]] = {}
    for user in sub_users:
        email = normalize_email(user.email)
        if email:
            by_email.setdefault(email, []).append(user)

    matched_sub_user_ids: dict[str, list[str]] = {}

    for staff in sorted(crm_staff, key=lambda s: s.person_id):
        result.reports["crm_people_directory"].append(
            {
                "id": staff.person_id,
                "name": staff.name,
                "email": normalize_email(staff.person_email) or "",
            }
        )

        candidates = candidate_emails(staff)
        hits = [(email, via, by_email.get(email, [])) for email, via in candidates]

        unique_hit = next(
            ((email, via, users[0]) for email, via, users in hits if len(users) == 1),
            None,
        )
        if unique_hit is not None:
            email, via, user = unique_hit
            stats.matched += 1
            if via == MATCH_VIA_PERSON_EMAIL:
                stats.matched_via_person_email += 1
            else:
                stats.matched_via_credential_username += 1
            if not user.is_active:
                stats.matched_inactive_sub_user += 1
            matched_sub_user_ids.setdefault(user.id, []).append(staff.person_id)
            result.reports["staff_map"].append(
                {
                    "crm_person_id": staff.person_id,
                    "crm_name": staff.name,
                    "crm_email": email,
                    "system_user_id": user.id,
                    "match_via": via,
                }
            )
            continue

        ambiguous_hit = next(
            ((email, users) for email, _via, users in hits if len(users) > 1), None
        )
        if ambiguous_hit is not None:
            email, users = ambiguous_hit
            stats.ambiguous += 1
            result.reports["ambiguous"].append(
                {
                    "crm_person_id": staff.person_id,
                    "crm_name": staff.name,
                    "crm_email": email,
                    "candidate_system_user_ids": ";".join(
                        sorted(user.id for user in users)
                    ),
                }
            )
            continue

        unmatched_row = {
            "crm_person_id": staff.person_id,
            "crm_name": staff.name,
            "crm_email": normalize_email(staff.person_email) or "",
            "credential_usernames": ";".join(staff.credential_usernames),
            "person_is_active": staff.person_is_active,
            "credential_is_active": staff.credential_is_active,
        }
        if staff.is_active:
            stats.unmatched_active += 1
            result.reports["unmatched_active"].append(unmatched_row)
        else:
            stats.unmatched_inactive += 1
            result.reports["unmatched_inactive"].append(unmatched_row)

    stats.sub_users_matched_by_multiple_crm_staff = sum(
        1 for person_ids in matched_sub_user_ids.values() if len(person_ids) > 1
    )
    return result


def _load_crm_staff(crm: Connection) -> list[CrmStaffRow]:
    rows = _rows(
        crm,
        """
        SELECT p.id::text AS person_id,
               p.first_name,
               p.last_name,
               p.display_name,
               p.email AS person_email,
               p.is_active AS person_is_active,
               c.username AS credential_username,
               c.is_active AS credential_is_active
        FROM people p
        JOIN user_credentials c ON c.person_id = p.id
        ORDER BY p.id, c.created_at, c.id
        """,
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["person_id"]).lower(), []).append(row)
    staff: list[CrmStaffRow] = []
    for person_id, person_rows in grouped.items():
        first = person_rows[0]
        usernames = tuple(
            str(row["credential_username"]).strip()
            for row in person_rows
            if row.get("credential_username")
            and str(row["credential_username"]).strip()
        )
        staff.append(
            CrmStaffRow(
                person_id=person_id,
                name=display_name(
                    first.get("display_name"),
                    first.get("first_name"),
                    first.get("last_name"),
                    first.get("person_email"),
                ),
                person_email=first.get("person_email"),
                credential_usernames=usernames,
                person_is_active=bool(first.get("person_is_active")),
                credential_is_active=any(
                    bool(row.get("credential_is_active")) for row in person_rows
                ),
            )
        )
    return sorted(staff, key=lambda s: s.person_id)


def _load_sub_system_users(sub: Connection) -> list[SystemUserRow]:
    rows = _rows(
        sub,
        """
        SELECT id::text AS id,
               first_name,
               last_name,
               display_name,
               email,
               user_type::text AS user_type,
               is_active
        FROM system_users
        ORDER BY id
        """,
    )
    return [
        SystemUserRow(
            id=str(row["id"]).lower(),
            email=row.get("email"),
            name=display_name(
                row.get("display_name"),
                row.get("first_name"),
                row.get("last_name"),
                row.get("email"),
            ),
            user_type=row.get("user_type"),
            is_active=bool(row.get("is_active")),
        )
        for row in rows
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="staff-map",
        help="Directory for staff_map.csv, per-class CSVs and summary.json.",
    )
    args = parser.parse_args()
    out = Path(args.out)

    sub_engine = _engine_from_env("SUB_DATABASE_URL")
    crm_engine = _engine_from_env("CRM_DATABASE_URL")
    with sub_engine.connect() as sub, crm_engine.connect() as crm:
        crm.execute(text("SET TRANSACTION READ ONLY"))
        crm_staff = _load_crm_staff(crm)
        crm.rollback()

        sub.execute(text("SET TRANSACTION READ ONLY"))
        sub_users = _load_sub_system_users(sub)
        sub.rollback()

    result = build_staff_map(crm_staff, sub_users)

    for name in REPORT_NAMES:
        _write_csv(out / f"{name}.csv", result.reports[name])

    report = {
        "output_dir": str(out),
        "stats": result.stats.as_dict(),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
