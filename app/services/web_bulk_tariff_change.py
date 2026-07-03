"""Web service helpers for admin bulk tariff change routes."""

from __future__ import annotations

import logging

from fastapi import Request
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.services.bulk_tariff_change import bulk_tariff_change

logger = logging.getLogger(__name__)


def _include_suspended(form: FormData) -> bool:
    """Read the opt-in "also change suspended subscriptions" checkbox.

    Unchecked (absent) means the historical active-only behavior; standard HTML
    checkbox truthy values map to opt-in.
    """
    raw = str(form.get("include_suspended") or "").strip().lower()
    return raw in {"1", "true", "on", "yes"}


def page_context(request: Request, db: Session) -> dict:
    """Build initial page context with offers list and subscription counts."""
    offers = bulk_tariff_change.list_offers(db)
    counts = bulk_tariff_change.count_by_offer(db)

    return {
        "offers": offers,
        "offer_counts": counts,
        "step": "select",
    }


def preview_context(request: Request, db: Session, form: FormData) -> dict:
    """After preview form submission, returns preview data."""
    source_offer_id = str(form.get("source_offer_id") or "").strip()
    target_offer_id = str(form.get("target_offer_id") or "").strip()
    include_suspended = _include_suspended(form)

    errors: list[str] = []
    if not source_offer_id:
        errors.append("Please select a source plan.")
    if not target_offer_id:
        errors.append("Please select a target plan.")
    if source_offer_id and target_offer_id and source_offer_id == target_offer_id:
        errors.append("Source and target plans must be different.")

    # Always need offers list for the form
    offers = bulk_tariff_change.list_offers(db)
    counts = bulk_tariff_change.count_by_offer(db)

    if errors:
        return {
            "offers": offers,
            "offer_counts": counts,
            "step": "select",
            "form_errors": errors,
            "form_source_offer_id": source_offer_id,
            "form_target_offer_id": target_offer_id,
            "form_include_suspended": include_suspended,
        }

    preview_data = bulk_tariff_change.preview(
        db,
        source_offer_id=source_offer_id,
        target_offer_id=target_offer_id,
        include_suspended=include_suspended,
    )

    return {
        "offers": offers,
        "offer_counts": counts,
        "step": "preview",
        "preview": preview_data,
        "form_source_offer_id": source_offer_id,
        "form_target_offer_id": target_offer_id,
        "form_include_suspended": include_suspended,
    }


def execute_context(request: Request, db: Session, form: FormData) -> dict:
    """Execute the change and return result context."""
    source_offer_id = str(form.get("source_offer_id") or "").strip()
    target_offer_id = str(form.get("target_offer_id") or "").strip()
    include_suspended = _include_suspended(form)

    offers = bulk_tariff_change.list_offers(db)
    counts = bulk_tariff_change.count_by_offer(db)

    try:
        result = bulk_tariff_change.execute(
            db,
            source_offer_id=source_offer_id,
            target_offer_id=target_offer_id,
            include_suspended=include_suspended,
        )
    except Exception as e:
        logger.error("Bulk tariff change execution failed: %s", e)
        return {
            "offers": offers,
            "offer_counts": counts,
            "step": "result",
            "result": {"changed": 0, "skipped": 0, "errors": 1, "failed_ids": []},
            "execution_error": str(e),
            "form_source_offer_id": source_offer_id,
            "form_target_offer_id": target_offer_id,
            "form_include_suspended": include_suspended,
        }

    # Refresh counts after the change
    updated_counts = bulk_tariff_change.count_by_offer(db)

    return {
        "offers": offers,
        "offer_counts": updated_counts,
        "step": "result",
        "result": result,
        "form_source_offer_id": source_offer_id,
        "form_target_offer_id": target_offer_id,
        "form_include_suspended": include_suspended,
    }
