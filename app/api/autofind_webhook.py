"""Autofind webhook endpoint for on-demand OLT scanning.

Provides an API endpoint that can be triggered by external systems
like Zabbix to initiate an autofind scan on a specific OLT.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.autofind_trigger import trigger_autofind_by_identifier

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/autofind", tags=["autofind-webhook"])

# Authentication token for webhook requests
AUTOFIND_WEBHOOK_TOKEN = os.getenv("AUTOFIND_WEBHOOK_TOKEN", "")


class AutofindTriggerRequest(BaseModel):
    """Request payload for triggering autofind."""

    model_config = ConfigDict(extra="forbid")

    olt: str = Field(
        description="OLT identifier - can be UUID, management IP address, or name"
    )
    force: bool = Field(
        default=False,
        description="If true, bypass cooldown and trigger immediately",
    )
    source: str | None = Field(
        default=None,
        description="Source identifier for logging (e.g., 'zabbix', 'manual')",
    )


class AutofindTriggerResponse(BaseModel):
    """Response for autofind trigger request."""

    status: str
    triggered: bool
    olt_id: str | None = None
    olt_name: str | None = None
    task_id: str | None = None
    message: str | None = None


def _validate_auth_token(token: str | None) -> None:
    """Validate the authentication token.

    Args:
        token: Token from X-Autofind-Token header

    Raises:
        HTTPException: If token is invalid or missing
    """
    if not AUTOFIND_WEBHOOK_TOKEN:
        # Token not configured - allow unauthenticated access
        # (should be secured at network level in this case)
        logger.warning(
            "autofind_webhook_no_token_configured: AUTOFIND_WEBHOOK_TOKEN not set - allowing unauthenticated access",
        )
        return

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Autofind-Token header",
        )

    if token != AUTOFIND_WEBHOOK_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
        )


@router.post("/webhook/trigger", response_model=AutofindTriggerResponse)
def trigger_autofind_webhook(
    payload: AutofindTriggerRequest,
    db: Session = Depends(get_db),
    x_autofind_token: str | None = Header(default=None, alias="X-Autofind-Token"),
):
    """Trigger an autofind scan for an OLT.

    This endpoint can be called by external systems (like Zabbix actions)
    to initiate an on-demand autofind scan. The OLT can be identified by
    its UUID, management IP address, or name.

    Includes cooldown protection to prevent excessive scanning - by default,
    each OLT can only be scanned once every 30 seconds. Use force=true to
    bypass the cooldown.
    """
    _validate_auth_token(x_autofind_token)

    source = payload.source or "webhook"

    logger.info(
        "autofind_webhook_received",
        extra={
            "olt_identifier": payload.olt,
            "force": payload.force,
            "source": source,
        },
    )

    result = trigger_autofind_by_identifier(
        db=db,
        identifier=payload.olt,
        source=source,
        force=payload.force,
    )

    if result.triggered:
        return AutofindTriggerResponse(
            status="ok",
            triggered=True,
            olt_id=result.olt_id,
            olt_name=result.olt_name,
            task_id=result.task_id,
            message="Autofind scan queued",
        )
    else:
        return AutofindTriggerResponse(
            status="ok",
            triggered=False,
            olt_id=result.olt_id,
            olt_name=result.olt_name,
            message=result.reason,
        )


@router.get("/webhook/health")
def autofind_webhook_health():
    """Health check endpoint for the autofind webhook.

    Returns basic health status for monitoring.
    """
    return {
        "status": "ok",
        "service": "autofind-webhook",
        "token_configured": bool(AUTOFIND_WEBHOOK_TOKEN),
    }
