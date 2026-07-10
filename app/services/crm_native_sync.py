"""Phase 3 sync-window adapter: CRM webhook events → NATIVE table deltas.

Transitional glue for the coexistence window (20-phase3-projects-sales.md
§4.2 step 4, PR 9): the backfill is done and CRM is still the writer for
projects / quotes / referrals, so sub's native tables must track CRM changes
the same way the mirrors do — the flip-day delta stays minutes. The whole
module is deleted at the Phase 3 contract (PR 15) together with the mirrors.

Split of responsibilities (webhook vs beat):

* This module (webhook path) applies only the THIN delta the CRM webhook
  payload actually carries — status + the timestamps the mirror parsers
  already trust — onto an EXISTING native row. CRM UUIDs are sub PKs for
  every Phase 3 table (§3.4), so the row lookup is a PK get.
* Everything else (new rows, line items, tasks, totals, …) needs the full
  CRM shape, which the portal API does not serve. That is the beat task
  ``app.tasks.crm_native_sync.pull_crm_phase3_native_delta`` — the backfill
  importer's watermark mode run in-process against the CRM DB — which is
  also the backstop when a webhook arrives before its native row exists.

Gated by ``crm_phase3_native_sync_enabled`` (projects domain, default OFF).
Called from the ``crm_webhooks`` branches IN ADDITION to the mirror apply —
mirrors keep syncing through the window per §4.2 (cheap read-flip rollback).
Best-effort by design: a failure here must never turn a webhook the mirror
already applied into a CRM retry, so ``apply_webhook_delta`` never raises.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.project import Project, ProjectStatus
from app.models.referral_native import Referral, ReferralRewardStatus, ReferralStatus
from app.models.sales import Quote, QuoteStatus
from app.services import settings_spec
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)

FLAG_KEY = "crm_phase3_native_sync_enabled"


def is_enabled(db: Session) -> bool:
    """Phase 3 sync-window flag: adapt CRM events into native rows."""
    return bool(settings_spec.resolve_value(db, SettingDomain.projects, FLAG_KEY))


# ── parsing helpers (mirror conventions) ────────────────────────────────────


def _to_dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _to_decimal(value: object):
    from decimal import Decimal, InvalidOperation

    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
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


_REFERRAL_STATUS_BY_EVENT = {
    "referral.captured": ReferralStatus.pending.value,
    "referral.qualified": ReferralStatus.qualified.value,
    "referral.rewarded": ReferralStatus.rewarded.value,
}


def _apply_referral(db: Session, event_type: str, body: dict) -> dict:
    crm_referral_id = str(body.get("referral_id") or body.get("id") or "").strip()
    if not crm_referral_id:
        return {"status": "skipped", "reason": "incomplete_payload"}

    row = _native_row(db, Referral, crm_referral_id)
    if row is None:
        return {"status": "skipped", "reason": "native_row_missing"}

    new_status = _REFERRAL_STATUS_BY_EVENT.get(event_type)
    if new_status:
        row.status = new_status
    now = datetime.now(UTC)
    if event_type == "referral.qualified" and row.qualified_at is None:
        row.qualified_at = _to_dt(body.get("qualified_at")) or now
    if event_type == "referral.rewarded":
        amount = _to_decimal(body.get("amount") or body.get("reward_amount"))
        if amount is not None:
            row.reward_amount = amount
            row.reward_currency = str(
                body.get("currency") or body.get("reward_currency") or "NGN"
            )
        # Native vocabulary is "issued" (§1.7) — NOT the CRM webhook's
        # "paid", which was the mirror-only value the doc flags at §1.7.
        row.reward_status = ReferralRewardStatus.issued.value
        if row.reward_issued_at is None:
            row.reward_issued_at = _to_dt(body.get("reward_issued_at")) or now
    db.commit()
    return {"status": "ok", "event": event_type}


_HANDLERS = {
    "project": _apply_project,
    "quote": _apply_quote,
    "referral": _apply_referral,
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
