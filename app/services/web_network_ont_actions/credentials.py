"""Credential management for ONT web actions."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import OntUnit
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.web_network_ont_actions._common import _log_action_audit

logger = logging.getLogger(__name__)


def resolve_stored_pppoe_password(db: Session, ont_id: str) -> str:
    """Decrypt and return the stored PPPoE password for an ONT."""
    from app.services.credential_crypto import decrypt_credential

    ont = db.get(OntUnit, ont_id)
    if not ont:
        return ""

    raw = getattr(ont, "pppoe_password", None)
    if not raw:
        return ""

    try:
        return decrypt_credential(raw) or ""
    except Exception:
        logger.warning("Failed to decrypt PPPoE password for ONT %s", ont_id)
        return ""


def reveal_stored_pppoe_password(
    db: Session, ont_id: str, *, request: Request | None = None
) -> tuple[str, bool]:
    """Return stored PPPoE password and audit the reveal action."""
    ont = db.get(OntUnit, ont_id)
    if not ont:
        return "", False

    password = resolve_stored_pppoe_password(db, ont_id)
    effective = resolve_effective_ont_config(db, ont)
    values = effective.get("values", {}) if isinstance(effective, dict) else {}
    _log_action_audit(
        db,
        request=request,
        action="reveal_pppoe_password",
        ont_id=ont_id,
        metadata={"username": str(values.get("pppoe_username") or "")},
    )
    return password, True
