"""HARD-DELETE the synthetic QA / Playwright / e2e test-artifact customers.

Soft-cancel (purge_qa_test_artifacts.py) does NOT remove them from the admin
customer list — that list shows every customer-type subscriber regardless of
status. This script physically deletes the artifact subscribers and every row
that references them, in FK-safe order.

IRREVERSIBLE. It deletes child rows across ~all referencing tables, including
the artifacts' synthetic invoices / payments / ledger entries / sessions /
notifications. It targets ONLY the same strictly-anchored artifact set as the
soft-delete script (reserved test-domain harness emails); real customers and the
WhatsApp / reseller-server / johndoe false-positives are excluded.

Mechanism: a generic catalog-driven cascade. For each table, it consults
pg_constraint for inbound FKs, recurses into NO-ACTION/RESTRICT children
(deleting them first), and lets CASCADE / SET NULL children be handled by the DB.

    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/one_off/hard_delete_qa_test_artifacts.py            # dry-run
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/one_off/hard_delete_qa_test_artifacts.py --apply     # delete
"""
from __future__ import annotations

import argparse
import re
import sys

from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal

# Same anchored patterns + exclusions as purge_qa_test_artifacts.py (single source
# of truth duplicated here so the destructive script is self-contained & auditable).
INCLUDE_RE = re.compile(
    r"^("
    r"qa\.[a-z]+\.\d+@example\.com"
    r"|e2e\.(user|agent)@example\.com"
    r"|admin@example\.com"
    r"|pppoe-ui-\d+@example\.com"
    r"|qa\.test(reseller|customer)@example\.invalid"
    r"|playwright-admin@example\.com"
    r"|codex\.test\+\d+@example\.com"
    r")$"
)
EXCLUDE_EMAILS = {"johndoe@example.com", "wanserver.reseller.20260618@example.com"}
EXCLUDE_RE = re.compile(r"^whatsapp--\d+@example\.invalid$")
MAX_AFFECTED = 30


def _artifact_ids(db) -> list[str]:
    rows = db.execute(
        text("SELECT id, email FROM subscribers WHERE email ILIKE '%@example.%'")
    ).fetchall()
    out = []
    for sid, email in rows:
        e = (email or "").lower()
        if e in EXCLUDE_EMAILS or EXCLUDE_RE.match(e):
            continue
        if INCLUDE_RE.match(e):
            out.append(str(sid))
    return out


def _fk_children(db, table: str):
    """Inbound FKs: (child_table, child_col, on_delete) referencing `table`."""
    return db.execute(
        text(
            "SELECT conrelid::regclass::text AS child_tbl, a.attname AS child_col, "
            "c.confdeltype AS od "
            "FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum = ANY(c.conkey) "
            "WHERE c.contype='f' AND c.confrelid = CAST(:t AS regclass)"
        ),
        {"t": table},
    ).fetchall()


def _pk_col(db, table: str) -> str | None:
    cols = db.execute(
        text(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = CAST(:t AS regclass) AND i.indisprimary"
        ),
        {"t": table},
    ).scalars().all()
    return cols[0] if len(cols) == 1 else None


def cascade_delete(db, table, ids, *, apply: bool, plan: list, path: tuple, sched: dict):
    """Recursively delete `ids` from `table`, children-first. `ids` are values of
    `table`'s primary key. `sched` memoizes already-scheduled pk values per table so
    rows reachable via multiple FK paths are processed (and counted) exactly once.
    Records (table, count) into `plan` in execution order."""
    pk = _pk_col(db, table)
    if pk:
        already = sched.setdefault(table, set())
        ids = [i for i in ids if i not in already]
        already.update(ids)
    if not ids:
        return
    for child_tbl, child_col, od in _fk_children(db, table):
        if od in (b"c", b"n", "c", "n"):
            continue  # CASCADE / SET NULL — DB handles on parent delete
        if child_tbl == table or child_tbl in path:
            continue  # self-ref / cycle — covered by the batch delete below
        child_pk = _pk_col(db, child_tbl)
        if child_pk:
            child_ids = db.execute(
                text(f'SELECT "{child_pk}" FROM {child_tbl} WHERE "{child_col}" = ANY(:ids)'),
                {"ids": ids},
            ).scalars().all()
            if child_ids:
                cascade_delete(
                    db, child_tbl, [str(x) for x in child_ids],
                    apply=apply, plan=plan, path=path + (table,), sched=sched,
                )
        else:
            # No single-column PK (association table): delete by FK condition.
            seen_keys = sched.setdefault(("nopk", child_tbl, child_col), set())
            key = frozenset(ids)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            n = db.execute(
                text(f'SELECT count(*) FROM {child_tbl} WHERE "{child_col}" = ANY(:ids)'),
                {"ids": ids},
            ).scalar()
            if n:
                if apply:
                    db.execute(
                        text(f'DELETE FROM {child_tbl} WHERE "{child_col}" = ANY(:ids)'),
                        {"ids": ids},
                    )
                plan.append((child_tbl, n))
    # delete this level
    if apply and pk:
        db.execute(text(f'DELETE FROM {table} WHERE "{pk}" = ANY(:ids)'), {"ids": ids})
    plan.append((table, len(ids)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="commit deletes (default: dry-run)")
    args = ap.parse_args()

    db_name = settings.database_url.rsplit("/", 1)[-1].split("?")[0]
    print(f"[{'APPLY' if args.apply else 'DRY-RUN'}] target DB: {db_name}")

    db = SessionLocal()
    try:
        ids = _artifact_ids(db)
        n = len(ids)
        print(f"Matched {n} artifact subscriber(s).")
        if n == 0:
            print("Nothing to do.")
            return 0
        if n > MAX_AFFECTED:
            print(f"ABORT: {n} > MAX_AFFECTED ({MAX_AFFECTED}). Review the include regex.")
            return 2

        plan: list = []
        cascade_delete(db, "subscribers", ids, apply=args.apply, plan=plan, path=(), sched={})

        # Aggregate per-table totals in execution order (children first).
        seen, order = {}, []
        for tbl, c in plan:
            if tbl not in seen:
                order.append(tbl)
            seen[tbl] = seen.get(tbl, 0) + c
        print("\nRows to delete (children-first execution order):")
        for tbl in order:
            print(f"  {seen[tbl]:>5}  {tbl}")
        print(f"  ----- total rows across {len(order)} tables: {sum(seen.values())}")

        if args.apply:
            db.commit()
            print(f"\nDELETED {n} artifact subscriber(s) and all dependent rows.")
        else:
            db.rollback()
            print("\nDRY-RUN — no changes. Re-run with --apply to delete.")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
