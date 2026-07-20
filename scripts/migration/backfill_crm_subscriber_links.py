#!/usr/bin/env python3
"""Phase 0 identity-link backfill for subscribers.crm_subscriber_id.

Inputs:
  SUB_DATABASE_URL=postgresql://...
  CRM_DATABASE_URL=postgresql://...

Reads CRM ``subscribers`` rows with ``external_system='selfcare'`` (their
``external_id`` is the sub subscriber id) and backfills the forward link
``subscribers.crm_subscriber_id`` plus ``metadata.crm_person_id`` and
``metadata.crm_alias_ids`` in sub. Writes only to sub; the CRM session is
always read-only. The backfill is dry-run by default. Pass ``--apply`` to
write.

Metadata key semantics:
  * ``crm_alias_ids`` is an OWNERSHIP claim ("these CRM subscriber rows are
    duplicates of me") — consumers (ticket import, crm_duplicate_merge) treat
    it as such.
  * ``crm_previous_ids`` is provenance only ("this row used to point here").

Decision rules:
  * CRM rows sharing one external_id: prefer the row already referenced by any
    sub ``crm_subscriber_id``, else the row with ``person_id`` set, else the
    newest ``updated_at``; losers are recorded in ``metadata.crm_alias_ids``.
  * Sub rows whose ``crm_subscriber_id`` disagrees with the chosen CRM row are
    repointed; the old UUID is preserved in ``metadata.crm_previous_ids``.
  * An id found in a row's ``crm_alias_ids`` that is another row's primary
    ``crm_subscriber_id`` is moved to ``crm_previous_ids``
    (``alias_conflict_repaired``) — it is not this row's duplicate.
  * Sub rows pointing at a CRM id with no selfcare row pointing back are left
    untouched and reported (``dangling``).
  * Would-be violations of the partial-unique index
    ``uq_subscribers_crm_subscriber_id`` are blocked and reported
    (``collision``): the current holder of a CRM id wins, then first-come by
    sub ``created_at``.
  * ``metadata.crm_person_id`` is set only where empty; a differing existing
    value is reported (``person_mismatch``) and never overwritten.

Apply runs in two phases: repointed rows are first NULLed in a single
committed statement so their freed ids can be re-granted without tripping the
immediate unique index, then the per-row updates run in batches. A crash
between the phases leaves repointed rows temporarily unlinked; a re-run plans
them as ``linked`` again.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

REPORT_ACTIONS = [
    "linked",
    "repointed",
    "alias_recorded",
    "alias_conflict_repaired",
    "person_linked",
    "person_mismatch",
    "dangling",
    "collision",
    "crm_duplicate_external_ids",
]

UPDATE_SUBSCRIBER_SQL = """
UPDATE subscribers
SET crm_subscriber_id = CAST(:crm_subscriber_id AS uuid),
    metadata = CAST(:metadata AS json)
WHERE id = CAST(:id AS uuid)
"""

CLEAR_REPOINTED_SQL = """
UPDATE subscribers
SET crm_subscriber_id = NULL
WHERE id::text = ANY(:ids)
"""


@dataclass(frozen=True)
class CrmLinkRow:
    id: str
    external_id: str
    person_id: str | None
    is_active: bool
    updated_at: datetime | None


@dataclass(frozen=True)
class SubLinkRow:
    id: str
    crm_subscriber_id: str | None
    metadata_text: str | None
    created_at: datetime | None


@dataclass(frozen=True)
class SubscriberUpdate:
    subscriber_id: str
    crm_subscriber_id: str | None
    metadata_json: str | None


@dataclass
class BackfillStats:
    sub_subscribers: int = 0
    crm_selfcare_rows: int = 0
    crm_duplicate_external_ids: int = 0
    crm_selfcare_without_sub_row: int = 0
    linked: int = 0
    repointed: int = 0
    alias_recorded: int = 0
    alias_conflict_repaired: int = 0
    person_linked: int = 0
    person_mismatch: int = 0
    dangling: int = 0
    collision: int = 0
    unchanged: int = 0
    unmatched: int = 0
    metadata_unmergeable: int = 0
    updates_planned: int = 0
    updates_applied: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "sub_subscribers": self.sub_subscribers,
            "crm_selfcare_rows": self.crm_selfcare_rows,
            "crm_duplicate_external_ids": self.crm_duplicate_external_ids,
            "crm_selfcare_without_sub_row": self.crm_selfcare_without_sub_row,
            "linked": self.linked,
            "repointed": self.repointed,
            "alias_recorded": self.alias_recorded,
            "alias_conflict_repaired": self.alias_conflict_repaired,
            "person_linked": self.person_linked,
            "person_mismatch": self.person_mismatch,
            "dangling": self.dangling,
            "collision": self.collision,
            "unchanged": self.unchanged,
            "unmatched": self.unmatched,
            "metadata_unmergeable": self.metadata_unmergeable,
            "updates_planned": self.updates_planned,
            "updates_applied": self.updates_applied,
        }


@dataclass
class BackfillPlan:
    updates: list[SubscriberUpdate] = field(default_factory=list)
    repointed_subscriber_ids: list[str] = field(default_factory=list)
    reports: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {name: [] for name in REPORT_ACTIONS}
    )
    stats: BackfillStats = field(default_factory=BackfillStats)


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


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


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


def _append_alias(alias_ids: list[str], chosen_id: str, alias_id: str | None) -> bool:
    """Append ``alias_id`` to ``alias_ids`` if new; never alias the chosen row."""
    if not alias_id or alias_id == chosen_id or alias_id in alias_ids:
        return False
    alias_ids.append(alias_id)
    return True


def _id_list(metadata: dict[str, Any], key: str) -> list[str]:
    raw = metadata.get(key)
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


def _repair_alias_conflicts(
    row_id: str,
    alias_ids: list[str],
    previous_ids: list[str],
    ownership: dict[str, str],
) -> list[str]:
    """Move alias ids that are another row's primary link to previous_ids.

    ``crm_alias_ids`` is an ownership claim; an id owned by a different sub
    row cannot be this row's duplicate — it is stale repoint provenance.
    """
    moved = [
        alias
        for alias in alias_ids
        if ownership.get(alias) is not None and ownership[alias] != row_id
    ]
    for alias in moved:
        alias_ids.remove(alias)
        if alias not in previous_ids:
            previous_ids.append(alias)
    return moved


def choose_crm_link_row(
    rows: list[CrmLinkRow], referenced_crm_ids: set[str]
) -> tuple[CrmLinkRow, list[CrmLinkRow]]:
    """Pick the canonical CRM row among duplicates sharing one external_id.

    Preference: already referenced by a sub ``crm_subscriber_id``, then
    ``person_id`` set, then newest ``updated_at``; final tie-break on id so
    the choice is fully deterministic.
    """

    def _preference(row: CrmLinkRow) -> tuple[bool, bool, datetime, str]:
        return (
            row.id in referenced_crm_ids,
            row.person_id is not None,
            row.updated_at or EPOCH,
            row.id,
        )

    winner = max(rows, key=_preference)
    losers = sorted((row for row in rows if row.id != winner.id), key=lambda r: r.id)
    return winner, losers


def build_plan(sub_rows: list[SubLinkRow], crm_rows: list[CrmLinkRow]) -> BackfillPlan:
    plan = BackfillPlan()
    stats = plan.stats
    stats.sub_subscribers = len(sub_rows)
    stats.crm_selfcare_rows = len(crm_rows)

    referenced_crm_ids = {
        row.crm_subscriber_id for row in sub_rows if row.crm_subscriber_id
    }
    by_external: dict[str, list[CrmLinkRow]] = {}
    for crm_row in crm_rows:
        external_id = _norm_id(crm_row.external_id)
        if external_id:
            by_external.setdefault(external_id, []).append(crm_row)

    winners: dict[str, CrmLinkRow] = {}
    losers_by_external: dict[str, list[CrmLinkRow]] = {}
    for external_id, candidates in by_external.items():
        winner, losers = choose_crm_link_row(candidates, referenced_crm_ids)
        winners[external_id] = winner
        losers_by_external[external_id] = losers
        if losers:
            stats.crm_duplicate_external_ids += 1
            plan.reports["crm_duplicate_external_ids"].append(
                {
                    "external_id": external_id,
                    "chosen_crm_subscriber_id": winner.id,
                    "loser_crm_subscriber_ids": ";".join(r.id for r in losers),
                    "row_count": len(candidates),
                }
            )

    sub_ids = {row.id for row in sub_rows}
    stats.crm_selfcare_without_sub_row = sum(
        1 for external_id in winners if external_id not in sub_ids
    )

    ordered = sorted(sub_rows, key=lambda r: (r.created_at or EPOCH, r.id))

    # Resolve the partial-unique index on crm_subscriber_id: a CRM id can be
    # held by at most one sub row. The current holder wins; freed ids (from
    # repoints) are handed out first-come by created_at via a fixpoint pass.
    ownership: dict[str, str] = {
        row.crm_subscriber_id: row.id for row in sub_rows if row.crm_subscriber_id
    }
    pending = [
        row
        for row in ordered
        if row.id in winners and winners[row.id].id != row.crm_subscriber_id
    ]
    accepted_moves: set[str] = set()
    progress = True
    while progress and pending:
        progress = False
        remaining: list[SubLinkRow] = []
        for row in pending:
            target = winners[row.id].id
            holder = ownership.get(target)
            if holder is None or holder == row.id:
                if (
                    row.crm_subscriber_id
                    and ownership.get(row.crm_subscriber_id) == row.id
                ):
                    ownership.pop(row.crm_subscriber_id)
                ownership[target] = row.id
                accepted_moves.add(row.id)
                progress = True
            else:
                remaining.append(row)
        pending = remaining
    collision_ids = {row.id for row in pending}

    def _report_repair(row_id: str, moved: list[str], current: str | None) -> None:
        stats.alias_conflict_repaired += 1
        plan.reports["alias_conflict_repaired"].append(
            {
                "subscriber_id": row_id,
                "crm_subscriber_id": current,
                "moved_to_previous_ids": ";".join(moved),
            }
        )

    for row in ordered:
        chosen = winners.get(row.id)
        current = row.crm_subscriber_id

        if chosen is None:
            if current:
                stats.dangling += 1
                plan.reports["dangling"].append(
                    {
                        "subscriber_id": row.id,
                        "crm_subscriber_id": current,
                    }
                )
            else:
                stats.unmatched += 1
            metadata = _json(row.metadata_text, {}) or {}
            if isinstance(metadata, dict):
                orphan_alias_ids = _id_list(metadata, "crm_alias_ids")
                orphan_previous_ids = _id_list(metadata, "crm_previous_ids")
                moved = _repair_alias_conflicts(
                    row.id, orphan_alias_ids, orphan_previous_ids, ownership
                )
                if moved:
                    _report_repair(row.id, moved, current)
                    merged = dict(metadata)
                    if orphan_alias_ids:
                        merged["crm_alias_ids"] = orphan_alias_ids
                    else:
                        merged.pop("crm_alias_ids", None)
                    merged["crm_previous_ids"] = orphan_previous_ids
                    plan.updates.append(
                        SubscriberUpdate(
                            subscriber_id=row.id,
                            crm_subscriber_id=current,
                            metadata_json=json.dumps(merged),
                        )
                    )
            continue

        if row.id in collision_ids:
            stats.collision += 1
            plan.reports["collision"].append(
                {
                    "subscriber_id": row.id,
                    "current_crm_subscriber_id": current,
                    "wanted_crm_subscriber_id": chosen.id,
                    "holding_subscriber_id": ownership.get(chosen.id),
                }
            )
            continue

        metadata = _json(row.metadata_text, {}) or {}
        if not isinstance(metadata, dict):
            stats.metadata_unmergeable += 1
            metadata = None

        link_changed = current != chosen.id
        metadata_changed = False
        alias_ids: list[str] = []
        previous_ids: list[str] = []
        if metadata is not None:
            alias_ids = _id_list(metadata, "crm_alias_ids")
            previous_ids = _id_list(metadata, "crm_previous_ids")

        if link_changed:
            if current is None:
                stats.linked += 1
                plan.reports["linked"].append(
                    {
                        "subscriber_id": row.id,
                        "crm_subscriber_id": chosen.id,
                        "crm_person_id": chosen.person_id,
                        "crm_is_active": chosen.is_active,
                        "crm_updated_at": _format_datetime(chosen.updated_at),
                    }
                )
            else:
                old_kept = metadata is not None and _append_alias(
                    previous_ids, chosen.id, current
                )
                if old_kept:
                    metadata_changed = True
                stats.repointed += 1
                plan.repointed_subscriber_ids.append(row.id)
                plan.reports["repointed"].append(
                    {
                        "subscriber_id": row.id,
                        "old_crm_subscriber_id": current,
                        "new_crm_subscriber_id": chosen.id,
                        "old_kept_as_previous": old_kept,
                    }
                )

        loser_aliases_added: list[str] = []
        if metadata is not None:
            loser_aliases_added = [
                loser.id
                for loser in losers_by_external.get(row.id, [])
                if _append_alias(alias_ids, chosen.id, loser.id)
            ]
        if loser_aliases_added:
            metadata_changed = True
            stats.alias_recorded += 1
            plan.reports["alias_recorded"].append(
                {
                    "subscriber_id": row.id,
                    "crm_subscriber_id": chosen.id,
                    "added_alias_ids": ";".join(loser_aliases_added),
                    "alias_ids_total": len(alias_ids),
                }
            )

        if metadata is not None:
            repaired = _repair_alias_conflicts(
                row.id, alias_ids, previous_ids, ownership
            )
            if repaired:
                metadata_changed = True
                _report_repair(row.id, repaired, chosen.id)

        if chosen.person_id and metadata is not None:
            existing_person = _norm_id(metadata.get("crm_person_id"))
            if existing_person is None:
                stats.person_linked += 1
                plan.reports["person_linked"].append(
                    {
                        "subscriber_id": row.id,
                        "crm_subscriber_id": chosen.id,
                        "crm_person_id": chosen.person_id,
                    }
                )
                metadata_changed = True
            elif existing_person != _norm_id(chosen.person_id):
                stats.person_mismatch += 1
                plan.reports["person_mismatch"].append(
                    {
                        "subscriber_id": row.id,
                        "crm_subscriber_id": chosen.id,
                        "existing_crm_person_id": existing_person,
                        "crm_person_id": chosen.person_id,
                    }
                )

        if not link_changed and not metadata_changed:
            stats.unchanged += 1
            continue

        metadata_json = row.metadata_text
        if metadata_changed and metadata is not None:
            merged = dict(metadata)
            if alias_ids:
                merged["crm_alias_ids"] = alias_ids
            else:
                merged.pop("crm_alias_ids", None)
            if previous_ids:
                merged["crm_previous_ids"] = previous_ids
            if chosen.person_id and _norm_id(metadata.get("crm_person_id")) is None:
                merged["crm_person_id"] = chosen.person_id
            metadata_json = json.dumps(merged)

        plan.updates.append(
            SubscriberUpdate(
                subscriber_id=row.id,
                crm_subscriber_id=chosen.id,
                metadata_json=metadata_json,
            )
        )

    stats.updates_planned = len(plan.updates)
    return plan


def _load_crm_link_rows(crm: Connection) -> list[CrmLinkRow]:
    rows = _rows(
        crm,
        """
        SELECT id::text AS id,
               external_id,
               person_id::text AS person_id,
               is_active,
               updated_at
        FROM subscribers
        WHERE external_system = 'selfcare'
          AND external_id IS NOT NULL
        ORDER BY updated_at, id
        """,
    )
    return [
        CrmLinkRow(
            id=str(row["id"]).lower(),
            external_id=str(row["external_id"]),
            person_id=_norm_id(row.get("person_id")),
            is_active=bool(row.get("is_active")),
            updated_at=_parse_datetime(row.get("updated_at")),
        )
        for row in rows
    ]


def _load_sub_link_rows(sub: Connection) -> list[SubLinkRow]:
    rows = _rows(
        sub,
        """
        SELECT id::text AS id,
               crm_subscriber_id::text AS crm_subscriber_id,
               metadata::text AS metadata,
               created_at
        FROM subscribers
        ORDER BY created_at, id
        """,
    )
    return [
        SubLinkRow(
            id=str(row["id"]).lower(),
            crm_subscriber_id=_norm_id(row.get("crm_subscriber_id")),
            metadata_text=row.get("metadata"),
            created_at=_parse_datetime(row.get("created_at")),
        )
        for row in rows
    ]


def _apply_updates(
    sub: Connection,
    updates: list[SubscriberUpdate],
    batch_size: int,
    repointed_subscriber_ids: list[str] | None = None,
) -> int:
    # Phase A: free the ids held by repointed rows in one committed statement,
    # otherwise a row can be granted an id before its current holder's repoint
    # executes (the unique index is immediate, so in-transaction order fails).
    if repointed_subscriber_ids:
        clear_trans = sub.begin()
        try:
            sub.execute(
                text(CLEAR_REPOINTED_SQL), {"ids": list(repointed_subscriber_ids)}
            )
            clear_trans.commit()
        except Exception:
            clear_trans.rollback()
            raise

    applied = 0
    trans = sub.begin()
    try:
        in_batch = 0
        for update in updates:
            sub.execute(
                text(UPDATE_SUBSCRIBER_SQL),
                {
                    "id": update.subscriber_id,
                    "crm_subscriber_id": update.crm_subscriber_id,
                    "metadata": update.metadata_json,
                },
            )
            applied += 1
            in_batch += 1
            if in_batch >= batch_size:
                trans.commit()
                trans = sub.begin()
                in_batch = 0
        trans.commit()
    except Exception:
        trans.rollback()
        raise
    return applied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Commit to sub after this many subscriber updates.",
    )
    parser.add_argument(
        "--out",
        default="crm-link-backfill",
        help="Directory for the summary JSON and per-action CSVs.",
    )
    args = parser.parse_args()

    out = Path(args.out)
    batch_size = max(1, args.batch_size)

    sub_engine = _engine_from_env("SUB_DATABASE_URL")
    crm_engine = _engine_from_env("CRM_DATABASE_URL")
    with sub_engine.connect() as sub, crm_engine.connect() as crm:
        crm.execute(text("SET TRANSACTION READ ONLY"))
        crm_rows = _load_crm_link_rows(crm)
        crm.rollback()

        read_trans = sub.begin()
        sub.execute(text("SET TRANSACTION READ ONLY"))
        sub_rows = _load_sub_link_rows(sub)
        read_trans.rollback()

        plan = build_plan(sub_rows, crm_rows)

        if args.apply and plan.updates:
            plan.stats.updates_applied = _apply_updates(
                sub, plan.updates, batch_size, plan.repointed_subscriber_ids
            )

    for name in REPORT_ACTIONS:
        _write_csv(out / f"{name}.csv", plan.reports[name])

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
