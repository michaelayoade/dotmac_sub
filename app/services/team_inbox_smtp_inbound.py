from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.services import team_inbox_rfc822, team_inbox_routing

logger = logging.getLogger(__name__)

SMTPController: Any = None
try:
    SMTPController = importlib.import_module("aiosmtpd.controller").Controller
except ModuleNotFoundError:
    SMTPController = None


@dataclass(frozen=True)
class SmtpInboundResult:
    kind: str
    conversation_id: str | None = None
    message_id: str | None = None
    reason: str | None = None


def normalize_recipient_set(
    values: set[str] | list[str] | tuple[str, ...] | None,
) -> set[str] | None:
    normalized = {
        address
        for address in (
            team_inbox_routing.normalize_email_address(value) for value in values or []
        )
        if address
    }
    return normalized or None


def envelope_matches_allowed_recipients(
    rcpt_to: list[str] | tuple[str, ...] | None,
    allowed_recipients: set[str] | None,
) -> bool:
    if not allowed_recipients:
        return True
    normalized_recipients = normalize_recipient_set(list(rcpt_to or [])) or set()
    return bool(normalized_recipients.intersection(allowed_recipients))


def handle_smtp_message(
    db: Session,
    *,
    mail_from: str | None,
    rcpt_to: list[str] | None,
    data: bytes,
    allowed_recipients: set[str] | None = None,
    fallback_service_team_id: str | None = None,
) -> SmtpInboundResult:
    if not envelope_matches_allowed_recipients(rcpt_to, allowed_recipients):
        return SmtpInboundResult(kind="skipped", reason="recipient_not_allowed")

    normalized_sender = team_inbox_routing.normalize_email_address(mail_from)
    if allowed_recipients and normalized_sender in allowed_recipients:
        return SmtpInboundResult(kind="skipped", reason="self_sender")

    try:
        result = team_inbox_rfc822.receive_rfc822_email(
            db,
            data,
            mail_from=mail_from,
            rcpt_to=rcpt_to or [],
            source="smtp",
            fallback_service_team_id=fallback_service_team_id,
        )
        db.commit()
        return SmtpInboundResult(
            kind=result.kind,
            conversation_id=result.conversation_id,
            message_id=result.message_id,
        )
    except Exception:
        db.rollback()
        logger.exception("team_inbox_smtp_message_failed")
        return SmtpInboundResult(kind="failed", reason="processing_error")


class TeamInboxSMTPHandler:
    def __init__(
        self,
        *,
        allowed_recipients: set[str] | None = None,
        fallback_service_team_id: str | None = None,
    ):
        self.allowed_recipients = normalize_recipient_set(allowed_recipients)
        self.fallback_service_team_id = fallback_service_team_id

    async def handle_DATA(self, server, session, envelope):  # noqa: N802
        rcpt_to = list(getattr(envelope, "rcpt_tos", None) or [])
        if not envelope_matches_allowed_recipients(rcpt_to, self.allowed_recipients):
            logger.info(
                "team_inbox_smtp_skip_recipient from=%s to=%s",
                getattr(envelope, "mail_from", None),
                ",".join(rcpt_to),
            )
            return "250 OK"

        db = SessionLocal()
        try:
            result = handle_smtp_message(
                db,
                mail_from=getattr(envelope, "mail_from", None),
                rcpt_to=rcpt_to,
                data=getattr(envelope, "content", None) or b"",
                allowed_recipients=self.allowed_recipients,
                fallback_service_team_id=self.fallback_service_team_id,
            )
            if result.kind == "failed":
                return "451 Temporary local processing error"
            return "250 OK"
        finally:
            db.close()


_SMTP_CONTROLLER: Any | None = None


def start_smtp_inbound_server() -> None:
    global _SMTP_CONTROLLER
    if SMTPController is None:
        logger.warning("team_inbox_smtp_unavailable reason=missing_aiosmtpd")
        return
    enabled = os.getenv("TEAM_INBOX_SMTP_INBOUND_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not enabled:
        return

    host = os.getenv("TEAM_INBOX_SMTP_INBOUND_HOST", "127.0.0.1")
    port = int(os.getenv("TEAM_INBOX_SMTP_INBOUND_PORT", "2525"))
    recipients = {
        value.strip()
        for value in os.getenv("TEAM_INBOX_SMTP_INBOUND_RECIPIENTS", "").split(",")
        if value.strip()
    }
    fallback_service_team_id = (
        os.getenv("TEAM_INBOX_SMTP_FALLBACK_SERVICE_TEAM_ID", "").strip() or None
    )
    controller = SMTPController(
        TeamInboxSMTPHandler(
            allowed_recipients=recipients or None,
            fallback_service_team_id=fallback_service_team_id,
        ),
        hostname=host,
        port=port,
    )
    controller.start()
    _SMTP_CONTROLLER = controller
    logger.info("team_inbox_smtp_server_started host=%s port=%s", host, port)


def stop_smtp_inbound_server() -> None:
    global _SMTP_CONTROLLER
    if _SMTP_CONTROLLER is None:
        return
    try:
        _SMTP_CONTROLLER.stop()
    except Exception:
        logger.exception("team_inbox_smtp_server_stop_failed")
    finally:
        _SMTP_CONTROLLER = None
