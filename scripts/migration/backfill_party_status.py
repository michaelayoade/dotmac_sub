#!/usr/bin/env python3
"""Phase 3 party backfill: subscribers.party_status + prospect subscriber rows.

Implements 20-phase3-projects-sales.md §3.2 / §1.9. Every CRM person referenced
by an **active** lead, quote, sales order, referral code or referral must have a
sub subscriber row before the Phase 3 vertical backfills can populate their
NOT NULL ``subscriber_id`` FKs.

Inputs:
  SUB_DATABASE_URL=postgresql://...
  CRM_DATABASE_URL=postgresql://...

For each referenced person, resolve to a sub subscriber via (in order):
  1. link key 4 — ``subscribers.metadata->>'crm_person_id'``
  2. link key 3 — ``people.metadata->>'selfcare_id'`` (sub subscriber id)
  3. the customer_identity_index email cascade (index rows plus subscribers'
     own normalized email column)
  4. the same cascade on normalized phone

Multiple candidates on one key are resolved deterministically (active first,
then earliest ``created_at``, then id) and reported (``ambiguous``).

Unresolved persons become **new subscriber rows**: ``status='new'``,
``party_status`` from ``people.party_status`` (default ``lead``), ``is_active``
per person, provenance ``metadata.crm_person_id`` — emitted to a creation CSV
for eyeballing (expect: mostly genuine prospects). Email stays non-unique
(doc 02 §3.2) so family/shared-email prospects import cleanly.

Resolved subscribers get ``party_status`` stamped from ``people.party_status``
where it is NULL — a differing existing value is reported
(``party_status_mismatch``) and never overwritten. ``metadata.crm_person_id``
is recorded where empty; a differing value is reported (``person_mismatch``)
and never overwritten (house rule from backfill_crm_subscriber_links.py).

The full ``crm_person_id -> subscriber_id`` map is written to
``person_subscriber_map.csv`` — the Phase 3 re-link artifact (§3.2 step 4)
consumed by the vertical backfills.

Dry-run by default; ``--apply`` writes (sub only — the CRM session is always
read-only). Idempotent: created rows carry ``metadata.crm_person_id`` so a
re-run resolves them via link key 4 and plans no work.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PARTY_STATUS_VALUES = ("lead", "contact", "customer", "subscriber")
DEFAULT_PARTY_STATUS = "lead"
DEFAULT_COUNTRY_CODE = "234"

RESOLUTION_CRM_PERSON_ID = "crm_person_id"
RESOLUTION_SELFCARE_ID = "selfcare_id"
RESOLUTION_EMAIL = "email"
RESOLUTION_PHONE = "phone"
RESOLUTION_CREATED = "created"

REPORT_ACTIONS = [
    "resolved",
    "created",
    "party_status_stamped",
    "party_status_mismatch",
    "person_link_recorded",
    "person_mismatch",
    "ambiguous",
]

MAP_FILENAME = "person_subscriber_map.csv"

UPDATE_SUBSCRIBER_SQL = """
UPDATE subscribers
SET party_status = :party_status,
    metadata = CAST(:metadata AS json)
WHERE id = CAST(:id AS uuid)
"""

INSERT_SUBSCRIBER_SQL = """
INSERT INTO subscribers (
    id, first_name, last_name, display_name, email, email_verified, phone,
    gender, status, user_type, is_active, marketing_opt_in,
    billing_enabled, captive_redirect_enabled, billing_mode,
    party_status, metadata, created_at, updated_at
) VALUES (
    CAST(:id AS uuid), :first_name, :last_name, :display_name, :email,
    false, :phone,
    CAST('unknown' AS gender), CAST('new' AS subscriberstatus),
    CAST('customer' AS usertype), :is_active, false,
    true, false, CAST('prepaid' AS billingmode),
    :party_status, CAST(:metadata AS json), now(), now()
)
"""


# ---------------------------------------------------------------------------
# Normalization (mirrors app/services/customer_identity_normalization.py; kept
# inline so the script has no app imports and the decision logic stays pure).
# ---------------------------------------------------------------------------


def normalize_email(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized or None


def normalize_phone(
    value: str | None, *, default_country_code: str = DEFAULT_COUNTRY_CODE
) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.replace("\u00a0", " ")
    lowered = raw.lower()
    for prefix in ("whatsapp:", "sms:", "tel:"):
        if lowered.startswith(prefix):
            raw = raw.split(":", 1)[1].strip()
            break
    has_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if has_plus:
        return f"+{digits}"
    if digits.startswith("00"):
        return f"+{digits[2:]}"
    if digits.startswith(default_country_code):
        return f"+{digits}"
    if digits.startswith("0") and len(digits) >= 10:
        return f"+{default_country_code}{digits[1:]}"
    if len(digits) == 10:
        return f"+{default_country_code}{digits}"
    return f"+{digits}"


# ---------------------------------------------------------------------------
# Row shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CrmPersonRow:
    id: str
    first_name: str
    last_name: str
    display_name: str | None
    email: str | None
    phone: str | None
    party_status: str | None
    is_active: bool
    selfcare_id: str | None
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubPartyRow:
    id: str
    email: str | None
    phone: str | None
    party_status: str | None
    metadata_text: str | None
    created_at: datetime | None
    is_active: bool = True


@dataclass(frozen=True)
class IdentityIndexRow:
    identity_type: str
    normalized_value: str
    subscriber_id: str


@dataclass(frozen=True)
class SubscriberPartyUpdate:
    subscriber_id: str
    party_status: str | None
    metadata_json: str | None


@dataclass(frozen=True)
class SubscriberInsert:
    subscriber_id: str
    first_name: str
    last_name: str
    display_name: str | None
    email: str
    phone: str | None
    party_status: str
    is_active: bool
    metadata_json: str


@dataclass
class BackfillStats:
    crm_people: int = 0
    sub_subscribers: int = 0
    identity_index_rows: int = 0
    resolved_crm_person_id: int = 0
    resolved_selfcare_id: int = 0
    resolved_email: int = 0
    resolved_phone: int = 0
    created: int = 0
    party_status_stamped: int = 0
    party_status_mismatch: int = 0
    person_link_recorded: int = 0
    person_mismatch: int = 0
    ambiguous: int = 0
    unchanged: int = 0
    metadata_unmergeable: int = 0
    updates_planned: int = 0
    inserts_planned: int = 0
    updates_applied: int = 0
    inserts_applied: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class BackfillPlan:
    updates: list[SubscriberPartyUpdate] = field(default_factory=list)
    inserts: list[SubscriberInsert] = field(default_factory=list)
    person_map: list[dict[str, Any]] = field(default_factory=list)
    reports: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {name: [] for name in REPORT_ACTIONS}
    )
    stats: BackfillStats = field(default_factory=BackfillStats)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text_value = str(value).strip()
        if not text_value:
            return None
        if text_value.endswith("Z"):
            text_value = text_value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text_value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _norm_id(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return normalized or None


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


def _party_status_or_default(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized in PARTY_STATUS_VALUES:
        return normalized
    return DEFAULT_PARTY_STATUS


def choose_subscriber(rows: list[SubPartyRow]) -> tuple[SubPartyRow, list[SubPartyRow]]:
    """Pick the canonical sub row among several candidates for one person.

    Preference: active rows first, then earliest ``created_at`` (the
    longest-standing account), final tie-break on id for determinism.
    """

    def _preference(row: SubPartyRow) -> tuple[bool, datetime, str]:
        return (not row.is_active, row.created_at or EPOCH, row.id)

    winner = min(rows, key=_preference)
    losers = sorted((row for row in rows if row.id != winner.id), key=lambda r: r.id)
    return winner, losers


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def build_plan(
    people: list[CrmPersonRow],
    sub_rows: list[SubPartyRow],
    identity_rows: list[IdentityIndexRow] | None = None,
) -> BackfillPlan:
    plan = BackfillPlan()
    stats = plan.stats
    identity_rows = identity_rows or []
    stats.crm_people = len(people)
    stats.sub_subscribers = len(sub_rows)
    stats.identity_index_rows = len(identity_rows)

    sub_by_id: dict[str, SubPartyRow] = {row.id: row for row in sub_rows}

    by_crm_person: dict[str, list[str]] = {}
    email_map: dict[str, set[str]] = {}
    phone_map: dict[str, set[str]] = {}
    for row in sub_rows:
        metadata = _json(row.metadata_text, {}) or {}
        if isinstance(metadata, dict):
            crm_person_id = _norm_id(metadata.get("crm_person_id"))
            if crm_person_id:
                by_crm_person.setdefault(crm_person_id, []).append(row.id)
        email = normalize_email(row.email)
        if email:
            email_map.setdefault(email, set()).add(row.id)
        phone = normalize_phone(row.phone)
        if phone:
            phone_map.setdefault(phone, set()).add(row.id)
    for identity in identity_rows:
        if identity.subscriber_id not in sub_by_id:
            continue
        value = _norm_id(identity.normalized_value)
        if not value:
            continue
        if identity.identity_type == "email":
            email_map.setdefault(value, set()).add(identity.subscriber_id)
        elif identity.identity_type == "phone":
            phone_map.setdefault(value, set()).add(identity.subscriber_id)

    # Working copy of (party_status, metadata) for rows the plan touches, so
    # two persons resolving to one subscriber see each other's decisions.
    working: dict[str, tuple[str | None, dict[str, Any] | None]] = {}
    dirty: set[str] = set()

    def _state(row: SubPartyRow) -> tuple[str | None, dict[str, Any] | None]:
        if row.id not in working:
            metadata = _json(row.metadata_text, {}) or {}
            if not isinstance(metadata, dict):
                stats.metadata_unmergeable += 1
                metadata = None
            working[row.id] = (row.party_status, metadata)
        return working[row.id]

    def _resolve_candidates(
        person: CrmPersonRow, candidate_ids: list[str], method: str
    ) -> SubPartyRow:
        candidates = [sub_by_id[sub_id] for sub_id in sorted(set(candidate_ids))]
        winner, losers = choose_subscriber(candidates)
        if losers:
            stats.ambiguous += 1
            plan.reports["ambiguous"].append(
                {
                    "crm_person_id": person.id,
                    "method": method,
                    "chosen_subscriber_id": winner.id,
                    "other_subscriber_ids": ";".join(row.id for row in losers),
                }
            )
        return winner

    ordered_people = sorted(people, key=lambda p: p.id)

    for person in ordered_people:
        resolution: str | None = None
        chosen: SubPartyRow | None = None

        candidate_ids = by_crm_person.get(person.id)
        if candidate_ids:
            chosen = _resolve_candidates(
                person, candidate_ids, RESOLUTION_CRM_PERSON_ID
            )
            resolution = RESOLUTION_CRM_PERSON_ID
            stats.resolved_crm_person_id += 1

        if chosen is None and person.selfcare_id:
            selfcare = _norm_id(person.selfcare_id)
            if selfcare and selfcare in sub_by_id:
                chosen = sub_by_id[selfcare]
                resolution = RESOLUTION_SELFCARE_ID
                stats.resolved_selfcare_id += 1

        if chosen is None:
            email = normalize_email(person.email)
            if email and email_map.get(email):
                chosen = _resolve_candidates(
                    person, list(email_map[email]), RESOLUTION_EMAIL
                )
                resolution = RESOLUTION_EMAIL
                stats.resolved_email += 1

        if chosen is None:
            phone = normalize_phone(person.phone)
            if phone and phone_map.get(phone):
                chosen = _resolve_candidates(
                    person, list(phone_map[phone]), RESOLUTION_PHONE
                )
                resolution = RESOLUTION_PHONE
                stats.resolved_phone += 1

        if chosen is None:
            # Genuine prospect: create a subscriber row (§3.2 step 3).
            new_id = str(uuid.uuid4())
            party_status = _party_status_or_default(person.party_status)
            insert = SubscriberInsert(
                subscriber_id=new_id,
                first_name=person.first_name,
                last_name=person.last_name,
                display_name=person.display_name,
                email=(person.email or "").strip(),
                phone=person.phone,
                party_status=party_status,
                is_active=person.is_active,
                metadata_json=json.dumps({"crm_person_id": person.id}),
            )
            plan.inserts.append(insert)
            stats.created += 1
            plan.reports["created"].append(
                {
                    "crm_person_id": person.id,
                    "subscriber_id": new_id,
                    "first_name": person.first_name,
                    "last_name": person.last_name,
                    "email": insert.email,
                    "phone": person.phone,
                    "party_status": party_status,
                    "person_is_active": person.is_active,
                    "sources": ";".join(person.sources),
                }
            )
            plan.person_map.append(
                {
                    "crm_person_id": person.id,
                    "subscriber_id": new_id,
                    "resolution": RESOLUTION_CREATED,
                    "sources": ";".join(person.sources),
                }
            )
            continue

        plan.reports["resolved"].append(
            {
                "crm_person_id": person.id,
                "subscriber_id": chosen.id,
                "method": resolution,
                "sources": ";".join(person.sources),
            }
        )
        plan.person_map.append(
            {
                "crm_person_id": person.id,
                "subscriber_id": chosen.id,
                "resolution": resolution,
                "sources": ";".join(person.sources),
            }
        )

        party_status, metadata = _state(chosen)
        changed = False

        person_party = _party_status_or_default(person.party_status)
        if party_status is None:
            party_status = person_party
            changed = True
            stats.party_status_stamped += 1
            plan.reports["party_status_stamped"].append(
                {
                    "subscriber_id": chosen.id,
                    "crm_person_id": person.id,
                    "party_status": person_party,
                }
            )
        elif party_status != person_party:
            stats.party_status_mismatch += 1
            plan.reports["party_status_mismatch"].append(
                {
                    "subscriber_id": chosen.id,
                    "crm_person_id": person.id,
                    "existing_party_status": party_status,
                    "crm_party_status": person_party,
                }
            )

        if metadata is not None:
            existing_person = _norm_id(metadata.get("crm_person_id"))
            if existing_person is None:
                metadata = dict(metadata)
                metadata["crm_person_id"] = person.id
                changed = True
                stats.person_link_recorded += 1
                plan.reports["person_link_recorded"].append(
                    {
                        "subscriber_id": chosen.id,
                        "crm_person_id": person.id,
                    }
                )
            elif existing_person != person.id:
                stats.person_mismatch += 1
                plan.reports["person_mismatch"].append(
                    {
                        "subscriber_id": chosen.id,
                        "existing_crm_person_id": existing_person,
                        "crm_person_id": person.id,
                    }
                )

        working[chosen.id] = (party_status, metadata)
        if changed:
            dirty.add(chosen.id)
        else:
            stats.unchanged += 1

    for sub_id in sorted(dirty):
        party_status, metadata = working[sub_id]
        original = sub_by_id[sub_id]
        metadata_json = (
            json.dumps(metadata) if metadata is not None else original.metadata_text
        )
        plan.updates.append(
            SubscriberPartyUpdate(
                subscriber_id=sub_id,
                party_status=party_status,
                metadata_json=metadata_json,
            )
        )

    stats.updates_planned = len(plan.updates)
    stats.inserts_planned = len(plan.inserts)
    return plan


# ---------------------------------------------------------------------------
# Load / apply
# ---------------------------------------------------------------------------


def _load_crm_people(crm: Connection) -> list[CrmPersonRow]:
    rows = _rows(
        crm,
        """
        WITH refs AS (
            SELECT person_id, 'lead' AS source
            FROM crm_leads WHERE is_active
            UNION ALL
            SELECT person_id, 'quote'
            FROM crm_quotes WHERE is_active
            UNION ALL
            SELECT person_id, 'sales_order'
            FROM sales_orders WHERE is_active
            UNION ALL
            SELECT person_id, 'referral_code'
            FROM referral_codes WHERE is_active
            UNION ALL
            SELECT referrer_person_id, 'referral_referrer'
            FROM referrals WHERE is_active
            UNION ALL
            SELECT referred_person_id, 'referral_referred'
            FROM referrals WHERE is_active AND referred_person_id IS NOT NULL
        )
        SELECT p.id::text AS id,
               p.first_name,
               p.last_name,
               p.display_name,
               p.email,
               p.phone,
               p.party_status::text AS party_status,
               p.is_active,
               p.metadata::text AS metadata,
               array_agg(DISTINCT r.source) AS sources
        FROM people p
        JOIN refs r ON r.person_id = p.id
        GROUP BY p.id
        ORDER BY p.id
        """,
    )
    people: list[CrmPersonRow] = []
    for row in rows:
        metadata = _json(row.get("metadata"), {}) or {}
        selfcare_id = None
        if isinstance(metadata, dict):
            selfcare_id = _norm_id(
                metadata.get("selfcare_id") or metadata.get("splynx_id")
            )
        people.append(
            CrmPersonRow(
                id=str(row["id"]).lower(),
                first_name=str(row.get("first_name") or ""),
                last_name=str(row.get("last_name") or ""),
                display_name=row.get("display_name"),
                email=row.get("email"),
                phone=row.get("phone"),
                party_status=row.get("party_status"),
                is_active=bool(row.get("is_active")),
                selfcare_id=selfcare_id,
                sources=tuple(sorted(row.get("sources") or [])),
            )
        )
    return people


def _load_sub_rows(sub: Connection) -> list[SubPartyRow]:
    rows = _rows(
        sub,
        """
        SELECT id::text AS id,
               email,
               phone,
               party_status,
               metadata::text AS metadata,
               created_at,
               is_active
        FROM subscribers
        ORDER BY created_at, id
        """,
    )
    return [
        SubPartyRow(
            id=str(row["id"]).lower(),
            email=row.get("email"),
            phone=row.get("phone"),
            party_status=row.get("party_status"),
            metadata_text=row.get("metadata"),
            created_at=_parse_datetime(row.get("created_at")),
            is_active=bool(row.get("is_active")),
        )
        for row in rows
    ]


def _load_identity_rows(sub: Connection) -> list[IdentityIndexRow]:
    rows = _rows(
        sub,
        """
        SELECT identity_type,
               normalized_value,
               subscriber_id::text AS subscriber_id
        FROM customer_identity_index
        WHERE identity_type IN ('email', 'phone')
        """,
    )
    return [
        IdentityIndexRow(
            identity_type=str(row["identity_type"]),
            normalized_value=str(row["normalized_value"]),
            subscriber_id=str(row["subscriber_id"]).lower(),
        )
        for row in rows
    ]


def _apply_plan(sub: Connection, plan: BackfillPlan, batch_size: int) -> None:
    trans = sub.begin()
    try:
        in_batch = 0
        for insert in plan.inserts:
            sub.execute(
                text(INSERT_SUBSCRIBER_SQL),
                {
                    "id": insert.subscriber_id,
                    "first_name": insert.first_name,
                    "last_name": insert.last_name,
                    "display_name": insert.display_name,
                    "email": insert.email,
                    "phone": insert.phone,
                    "is_active": insert.is_active,
                    "party_status": insert.party_status,
                    "metadata": insert.metadata_json,
                },
            )
            plan.stats.inserts_applied += 1
            in_batch += 1
            if in_batch >= batch_size:
                trans.commit()
                trans = sub.begin()
                in_batch = 0
        for update in plan.updates:
            sub.execute(
                text(UPDATE_SUBSCRIBER_SQL),
                {
                    "id": update.subscriber_id,
                    "party_status": update.party_status,
                    "metadata": update.metadata_json,
                },
            )
            plan.stats.updates_applied += 1
            in_batch += 1
            if in_batch >= batch_size:
                trans.commit()
                trans = sub.begin()
                in_batch = 0
        trans.commit()
    except Exception:
        trans.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Commit to sub after this many subscriber writes.",
    )
    parser.add_argument(
        "--out",
        default="party-status-backfill",
        help="Directory for the summary JSON, per-action CSVs and the "
        "crm_person_id -> subscriber_id map artifact.",
    )
    args = parser.parse_args()

    out = Path(args.out)
    batch_size = max(1, args.batch_size)

    sub_engine = _engine_from_env("SUB_DATABASE_URL")
    crm_engine = _engine_from_env("CRM_DATABASE_URL")
    with sub_engine.connect() as sub, crm_engine.connect() as crm:
        crm.execute(text("SET TRANSACTION READ ONLY"))
        people = _load_crm_people(crm)
        crm.rollback()

        read_trans = sub.begin()
        sub.execute(text("SET TRANSACTION READ ONLY"))
        sub_rows = _load_sub_rows(sub)
        identity_rows = _load_identity_rows(sub)
        read_trans.rollback()

        plan = build_plan(people, sub_rows, identity_rows)

        if args.apply and (plan.inserts or plan.updates):
            _apply_plan(sub, plan, batch_size)

    for name in REPORT_ACTIONS:
        _write_csv(out / f"{name}.csv", plan.reports[name])
    _write_csv(out / MAP_FILENAME, plan.person_map)

    report = {
        "apply": args.apply,
        "batch_size": batch_size,
        "output_dir": str(out),
        "stats": plan.stats.as_dict(),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
