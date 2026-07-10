"""Celery task: Phase 3 sync-window delta pull (CRM DB → native tables).

Transitional glue for the coexistence window (20-phase3-projects-sales.md
§4.2 step 4, PR 9); deleted at the Phase 3 contract (PR 15) with the rest of
the adapter. The webhook side (``app.services.crm_native_sync``) applies thin
status deltas; this beat is the full-shape source: the native tables need
whole CRM rows (line items, tasks, totals, …) that the portal API does not
serve, so — instead of new sync logic — it runs the backfill importer's
watermark mode (``scripts/migration/import_crm_phase3.run_import``) in
process against the CRM DB (``CRM_DATABASE_URL``, tunnel DSN). Everything is
idempotent ``ON CONFLICT`` upserts keyed on the CRM UUID (§3.4), so re-runs
and webhook/beat overlap are safe.

State: point ``CRM_PHASE3_SYNC_STATE_FILE`` at the state file the final
backfill run wrote so the first delta continues from its watermarks. Without
one (or after losing it) the run degrades to a full re-upsert pass — slow
but correct. Blockers (unresolvable NOT NULL subscriber links, unique
collisions) roll the whole run back, exactly like the CLI importer; they
mean the party map drifted and need operator attention, not a silent skip.

Importing script code into an app task is deliberate here: the importer is
the tested implementation of the CRM→native mapping and this beat dies with
it in PR 15 — a shared-module refactor would outlive its only second caller.
The import is lazy so web/worker startup never pays for it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.celery_app import celery_app
from app.db import task_session

logger = logging.getLogger(__name__)

_DEFAULT_STATE_FILE = str(
    Path(__file__).resolve().parents[2] / "var" / "phase3-native-sync-state.json"
)


def _state_file() -> str:
    return os.getenv("CRM_PHASE3_SYNC_STATE_FILE") or _DEFAULT_STATE_FILE


def _overlap_seconds() -> int:
    try:
        return max(0, int(os.getenv("CRM_PHASE3_SYNC_OVERLAP_SECONDS") or ""))
    except ValueError:
        return 600


def _run_delta(crm_dsn: str, state_file: str, overlap_seconds: int) -> dict:
    """One watermark-mode importer pass: CRM DB (read-only) → sub native rows."""
    from sqlalchemy import create_engine, text

    from app.db import get_engine
    from scripts.migration.import_crm_phase3 import (
        RunContext,
        _load_party_map_from_sub,
        _load_ticket_rekey_map,
        merge_party_maps,
        run_import,
        write_state_keys,
    )
    from scripts.migration.import_crm_tickets_phase1 import _load_subscriber_map

    sub_engine = get_engine()
    crm_engine = create_engine(crm_dsn, pool_pre_ping=True)
    try:
        with sub_engine.connect() as sub, crm_engine.connect() as crm:
            sub_trans = sub.begin()
            crm.execute(text("SET TRANSACTION READ ONLY"))
            try:
                ctx = RunContext(
                    apply=True,
                    state_file=state_file,
                    overlap_seconds=overlap_seconds,
                    # No CSV artifacts in-process: the party map comes from the
                    # links already stamped in sub (PR 1's backfill wrote them);
                    # the staff map only feeds an informational CSV, so empty.
                    party_map=merge_party_maps({}, _load_party_map_from_sub(sub)),
                    subscriber_map=_load_subscriber_map(sub),
                    staff_map={},
                    ticket_map=_load_ticket_rekey_map(sub),
                )
                stats = run_import(
                    sub=sub, crm=crm, ctx=ctx, validate_lead_fk_flag=False
                )
            except Exception:
                sub_trans.rollback()
                crm.rollback()
                raise
            if stats.blockers:
                sub_trans.rollback()
            else:
                sub_trans.commit()
                write_state_keys(state_file, stats.watermarks)
            crm.rollback()
    finally:
        crm_engine.dispose()
        sub_engine.dispose()

    created = sum(step.get("created", 0) for step in stats.steps.values())
    updated = sum(step.get("updated", 0) for step in stats.steps.values())
    if stats.blockers:
        logger.error(
            "crm_phase3_native_delta_blocked blockers=%s first=%s",
            len(stats.blockers),
            stats.blockers[0],
        )
        return {"status": "blocked", "blockers": len(stats.blockers)}
    logger.info("crm_phase3_native_delta_ok created=%s updated=%s", created, updated)
    return {"status": "ok", "created": created, "updated": updated, "blockers": 0}


@celery_app.task(name="app.tasks.crm_native_sync.pull_crm_phase3_native_delta")
def pull_crm_phase3_native_delta() -> dict:
    """Pull CRM changes since the last watermark into the native Phase 3 tables.

    Gated by ``crm_phase3_native_sync_enabled`` (same flag as the webhook
    adapter and the beat entry itself — this in-task check is the belt to the
    scheduler's braces, matching the crm.ticket_pull precedent)."""
    from app.services import crm_native_sync

    with task_session() as db:
        enabled = crm_native_sync.is_enabled(db)
    if not enabled:
        return {"status": "skipped", "reason": "native_sync_disabled"}

    crm_dsn = os.getenv("CRM_DATABASE_URL")
    if not crm_dsn:
        logger.error("crm_phase3_native_delta_missing_crm_database_url")
        return {"status": "error", "reason": "crm_database_url_not_configured"}

    return _run_delta(crm_dsn, _state_file(), _overlap_seconds())
