"""Config snapshot management for ONT web actions."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.services.web_network_ont_actions._common import _config_snapshot_service


def capture_config_snapshot_list_context(
    db: Session,
    *,
    ont_id: str,
    label: str | None,
    limit: int = 5,
) -> tuple[dict[str, object], str | None]:
    """Capture a config snapshot and return refreshed list context plus error."""
    snapshots_service = _config_snapshot_service()
    error_msg: str | None = None
    try:
        snapshots_service.capture(db, ont_id, label=label)
    except HTTPException as exc:
        error_msg = str(exc.detail)
    return {
        "ont_id": ont_id,
        "config_snapshots": snapshots_service.list_for_ont(db, ont_id, limit=limit),
    }, error_msg


def config_snapshot_detail_context(
    db: Session,
    *,
    ont_id: str,
    snapshot_id: str,
) -> dict[str, object]:
    """Return context for a single ONT config snapshot detail."""
    snapshot = _config_snapshot_service().get(db, snapshot_id, ont_id=ont_id)
    return {"snapshot": snapshot}


def delete_config_snapshot_list_context(
    db: Session,
    *,
    ont_id: str,
    snapshot_id: str,
    limit: int = 5,
) -> dict[str, object]:
    """Delete a config snapshot and return refreshed list context."""
    snapshots_service = _config_snapshot_service()
    snapshots_service.delete(db, snapshot_id, ont_id=ont_id)
    return {
        "ont_id": ont_id,
        "config_snapshots": snapshots_service.list_for_ont(db, ont_id, limit=limit),
    }
