"""sync-window adapter: CRM webhook events → NATIVE table deltas.

Transitional glue for the projects-and-sales coexistence window: the backfill
is done and CRM is still the writer for projects and quotes, so sub's native
tables must track CRM changes the same way the mirrors do — the flip-day delta
stays minutes. The whole module is deleted at the cutover contract together
with the mirrors.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.project import Project, ProjectStatus
from app.models.sales import Quote, QuoteStatus
from app.services import control_registry
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


def is_enabled(db: Session) -> bool:
    """sync-window flag: adapt CRM events into native rows."""
    return control_registry.is_enabled(db, "crm.phase3_native_sync")


# ── parsing helpers (mirror conventions) ────────────────────────────────────


def _to_dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _native_row(db: Session, model, raw_id: str):
    try:
        return db.get(model, coerce_uuid(raw_id))
    except (ValueError, TypeError):
        return None


# ── per-vertical thin deltas ─────────────────────────────────────────────────


def _apply_project(db: Session, event_type: str, body: dict) -> dict:
    crm_project_id = str(body.get("project_id") or body.get("id") or "").strip()
    if not crm_project_id:
        return {"status": "skipped", "reason": "incomplete_payload"}

    if event_type.startswith("project_task."):
        # Task events carry only ids — no status vocabulary worth guessing
        # at. The delta beat re-pulls the full task rows within minutes.
        return {"status": "skipped", "reason": "task_events_via_delta_beat"}

    row = _native_row(db, Project, crm_project_id)
    if row is None:
        # Not backfilled/created natively yet — the delta beat creates it
        # with the full CRM shape (this thin payload can't).
        return {"status": "skipped", "reason": "native_row_missing"}

    status = str(body.get("status") or body.get("to_status") or "").strip()
    if not status:
        status = {
            "project.completed": ProjectStatus.completed.value,
            "project.canceled": ProjectStatus.canceled.value,
        }.get(event_type, "")
    if status in {s.value for s in ProjectStatus}:
        row.status = status
    elif status:
        logger.warning(
            "crm_native_sync_unknown_project_status project=%s status=%s",
            crm_project_id,
            status,
        )
    if event_type == "project.completed" and row.completed_at is None:
        row.completed_at = _to_dt(body.get("completed_at")) or datetime.now(UTC)
    db.commit()
    return {"status": "ok", "event": event_type}


def _apply_quote(db: Session, event_type: str, body: dict) -> dict:
    crm_quote_id = str(body.get("quote_id") or body.get("id") or "").strip()
    if not crm_quote_id:
        return {"status": "skipped", "reason": "incomplete_payload"}

    row = _native_row(db, Quote, crm_quote_id)
    if row is None:
        return {"status": "skipped", "reason": "native_row_missing"}

    status = str(body.get("status") or "").strip()
    if not status:
        status = {
            "quote.accepted": QuoteStatus.accepted.value,
            "quote.rejected": QuoteStatus.rejected.value,
        }.get(event_type, "")
    if status in {s.value for s in QuoteStatus}:
        row.status = status
    elif status:
        logger.warning(
            "crm_native_sync_unknown_quote_status quote=%s status=%s",
            crm_quote_id,
            status,
        )
    db.commit()
    return {"status": "ok", "event": event_type}


_HANDLERS = {
    "project": _apply_project,
    "quote": _apply_quote,
}


def apply_webhook_delta(
    db: Session, vertical: str, event_type: str, body: dict
) -> dict:
    """Apply a CRM lifecycle event's thin delta to the native tables.

    Never raises — the mirror apply already committed and the CRM must not
    retry (the dedup claim would drop the redelivery anyway); the delta beat
    is the backstop for anything lost here.
    """
    try:
        if not is_enabled(db):
            return {"status": "skipped", "reason": "native_sync_disabled"}
        handler = _HANDLERS.get(vertical)
        if handler is None:
            return {"status": "skipped", "reason": "unknown_vertical"}
        return handler(db, event_type, body)
    except Exception as exc:  # noqa: BLE001 — must never fail the webhook
        db.rollback()
        logger.warning(
            "crm_native_sync_webhook_delta_failed vertical=%s event=%s: %s",
            vertical,
            event_type,
            exc,
        )
        return {"status": "error", "reason": "adapter_failed"}
