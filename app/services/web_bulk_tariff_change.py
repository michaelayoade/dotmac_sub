"""Web service helpers for admin bulk tariff change routes."""
from __future__ import annotations

import logging
from datetime import date

from fastapi import Request
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.services.bulk_tariff_change import bulk_tariff_change

logger = logging.getLogger(__name__)


def page_context(request: Request, db: Session) -> dict:
    """Build initial page context with offers list and subscription counts."""
    offers = bulk_tariff_change.list_offers(db)
    counts = bulk_tariff_change.count_by_offer(db)
    today = date.today().isoformat()

    return {
        "offers": offers,
        "offer_counts": counts,
        "today": today,
        "step": "select",
    }


def preview_context(request: Request, db: Session, form: FormData) -> dict:
    """After preview form submission, returns preview data."""
    source_offer_id = str(form.get("source_offer_id") or "").strip()
    target_offer_id = str(form.get("target_offer_id") or "").strip()
    start_date_str = str(form.get("start_date") or "").strip()
    ignore_balance = form.get("ignore_balance") == "on"

    errors: list[str] = []
    if not source_offer_id:
        errors.append("Please select a source plan.")
    if not target_offer_id:
        errors.append("Please select a target plan.")
    if source_offer_id and target_offer_id and source_offer_id == target_offer_id:
        errors.append("Source and target plans must be different.")
    if not start_date_str:
        errors.append("Please select a start date.")

    parsed_date = date.today()
    if start_date_str:
        try:
            parsed_date = date.fromisoformat(start_date_str)
        except ValueError:
            errors.append("Invalid date format.")

    # Always need offers list for the form
    offers = bulk_tariff_change.list_offers(db)
    counts = bulk_tariff_change.count_by_offer(db)
    today = date.today().isoformat()

    if errors:
        return {
            "offers": offers,
            "offer_counts": counts,
            "today": today,
            "step": "select",
            "form_errors": errors,
            "form_source_offer_id": source_offer_id,
            "form_target_offer_id": target_offer_id,
            "form_start_date": start_date_str,
            "form_ignore_balance": ignore_balance,
        }

    preview_data = bulk_tariff_change.preview(
        db,
        source_offer_id=source_offer_id,
        target_offer_id=target_offer_id,
        start_date=parsed_date,
        ignore_balance=ignore_balance,
    )

    return {
        "offers": offers,
        "offer_counts": counts,
        "today": today,
        "step": "preview",
        "preview": preview_data,
        "form_source_offer_id": source_offer_id,
        "form_target_offer_id": target_offer_id,
        "form_start_date": start_date_str,
        "form_ignore_balance": ignore_balance,
    }


def execute_context(request: Request, db: Session, form: FormData) -> dict:
    """Execute the change and return result context."""
    source_offer_id = str(form.get("source_offer_id") or "").strip()
    target_offer_id = str(form.get("target_offer_id") or "").strip()
    start_date_str = str(form.get("start_date") or "").strip()
    ignore_balance = form.get("ignore_balance") == "on"

    parsed_date = date.today()
    if start_date_str:
        try:
            parsed_date = date.fromisoformat(start_date_str)
        except ValueError:
            parsed_date = date.today()

    offers = bulk_tariff_change.list_offers(db)
    counts = bulk_tariff_change.count_by_offer(db)
    today = date.today().isoformat()

    try:
        result = bulk_tariff_change.execute(
            db,
            source_offer_id=source_offer_id,
            target_offer_id=target_offer_id,
            start_date=parsed_date,
            ignore_balance=ignore_balance,
        )
    except Exception as e:
        logger.error("Bulk tariff change execution failed: %s", e)
        return {
            "offers": offers,
            "offer_counts": counts,
            "today": today,
            "step": "result",
            "result": {"changed": 0, "skipped": 0, "errors": 1},
            "execution_error": str(e),
            "form_source_offer_id": source_offer_id,
            "form_target_offer_id": target_offer_id,
            "form_start_date": start_date_str,
            "form_ignore_balance": ignore_balance,
        }

    # Refresh counts after the change
    updated_counts = bulk_tariff_change.count_by_offer(db)

    return {
        "offers": offers,
        "offer_counts": updated_counts,
        "today": today,
        "step": "result",
        "result": result,
        "form_source_offer_id": source_offer_id,
        "form_target_offer_id": target_offer_id,
        "form_start_date": start_date_str,
        "form_ignore_balance": ignore_balance,
    }
