"""Enumeration-safe transport boundary for anonymous recovery requests."""

from __future__ import annotations

import logging

from app.metrics import ANONYMOUS_RECOVERY_FAILURES
from app.services import credential_recovery
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


def request_password_recovery(
    *, command: credential_recovery.RequestPasswordRecoveryCommand
) -> None:
    """Map owner/session failures to the same anonymous accepted response.

    This adapter owns only a dedicated session lifecycle. The recovery owner
    retains transaction authority; no caller session or pending work is touched.
    Exact-principal and authenticated recovery commands retain their errors.
    """
    try:
        with db_session_adapter.owner_command_session() as db:
            credential_recovery.request_password_recovery(db=db, command=command)
    except Exception:
        # Never include the exception, submitted identifier, request context,
        # or traceback: database errors can contain bound recovery parameters.
        try:
            ANONYMOUS_RECOVERY_FAILURES.inc()
        except Exception:
            pass
        try:
            logger.warning("anonymous_password_recovery_failed")
        except Exception:
            pass
