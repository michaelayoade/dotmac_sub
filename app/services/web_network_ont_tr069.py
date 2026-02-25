"""Service helpers for ONT TR-069 detail web routes."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.network.ont_tr069 import OntTR069, TR069Summary

logger = logging.getLogger(__name__)


def tr069_tab_data(db: Session, ont_id: str) -> dict[str, object]:
    """Build context for the TR-069 tab partial template.

    Args:
        db: Database session.
        ont_id: OntUnit ID.

    Returns:
        Template context dict with TR-069 summary data.
    """
    summary: TR069Summary = OntTR069.get_device_summary(db, ont_id)
    return {
        "tr069": summary,
        "tr069_available": summary.available,
    }
