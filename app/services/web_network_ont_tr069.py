"""Service helpers for ONT TR-069 detail web routes."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.acs_client import create_acs_state_reader

logger = logging.getLogger(__name__)


def tr069_tab_data(db: Session, ont_id: str) -> dict[str, object]:
    """Build context for the TR-069 tab partial template.

    Args:
        db: Database session.
        ont_id: OntUnit ID.

    Returns:
        Template context dict with TR-069 summary data.
    """
    summary = create_acs_state_reader().get_device_summary(
        db,
        ont_id,
        persist_observed_runtime=True,
    )
    return {
        "tr069": summary,
        "tr069_available": summary.available,
    }
