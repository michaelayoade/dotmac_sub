"""Service helpers for public CRM webhooks - CRM module removed."""

import logging

logger = logging.getLogger(__name__)

def whatsapp_webhook(payload, db):
    """CRM removed - no-op."""
    return {"status": "ok", "message": "CRM module removed"}

def email_webhook(payload, db):
    """CRM removed - no-op."""
    return {"status": "ok", "message": "CRM module removed"}
