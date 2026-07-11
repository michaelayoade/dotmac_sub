"""Vendor project-stub relay: push sub-native project stubs into CRM ``projects``.

Transitional Phase 3 glue (doc 20 §4.3 step 6, risk #6). After the write-flip,
new projects are native to sub only, but CRM's vendor portal + dotmac_field
vendor mode still create ``installation_projects`` rows (and
``wireless_surveys`` / ``material_requests`` / ``expense_requests``) with a
NOT NULL FK to CRM ``projects.id``. Until the Phase 5 vendor port, sub relays a
minimal stub row into CRM's ``projects`` table for every project whose type is
vendor-relevant, so those CRM-side FKs keep resolving.

Design (mirrors ``crm_webhook.push_subscriber_change`` + ``crm_ticket_push``):
the stub is signed with ``crm_webhook_secret`` and POSTed to CRM's public
receiver via ``CRMClient.post_signed_webhook`` (``X-Selfcare-Signature`` over the
exact bytes). The trigger is the decoupled ``VendorProjectRelayHandler`` event
handler, so the projects service stays untouched — the whole feature is one
flag + one handler + one task + one receiver, deleted wholesale at Phase 5.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import settings
from app.models.domain_settings import SettingDomain
from app.models.project import Project
from app.services.crm_client import get_crm_client

logger = logging.getLogger(__name__)

RELAY_WEBHOOK_PATH = "/webhooks/crm/projects/relay"

# Vendor-relevant project types — the exact set CRM's admin project form uses to
# decide whether to auto-create an ``installation_projects`` wrapper
# (dotmac_crm app/web/admin/projects.py:1013 ``installation_types``). Only these
# types drive a vendor bid / dotmac_field vendor job, so only these need a
# CRM-side stub after the write-flip.
VENDOR_RELEVANT_PROJECT_TYPES = frozenset(
    {"fiber_optics_installation", "air_fiber_installation"}
)

# Project lifecycle event names (carried in ``EventType.custom`` payload["name"],
# risk #13) that should drive a relay push.
RELAY_EVENT_NAMES = frozenset(
    {"project.created", "project.updated", "project.completed", "project.canceled"}
)


def coerce_uuid(value: str | UUID | None) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def relay_enabled(db: Session) -> bool:
    """Phase 3 write-flip flag (default OFF; flips ON at the write-flip window).

    Follows the read-flip flag-family convention
    (``projects_native_read_enabled`` …). OFF — nothing is relayed; ON —
    vendor-relevant project create/update pushes a stub into CRM ``projects``.
    """
    from app.services import settings_spec

    return bool(
        settings_spec.resolve_value(
            db, SettingDomain.projects, "vendor_project_relay_enabled"
        )
    )


def is_vendor_relevant(project: Project) -> bool:
    return (project.project_type or "") in VENDOR_RELEVANT_PROJECT_TYPES


def build_relay_payload(db: Session, project: Project) -> dict[str, Any]:
    """Minimal stub the CRM upserts into its ``projects`` table.

    Stub fields only (id, name, status, project_type, customer_address, region)
    plus provenance: ``source='sub_relay'`` (the CRM no-clobber guard) and the
    resolved CRM subscriber ref. The id is the sub project UUID so every CRM-side
    FK value already matches (doc 20 §3.4 shared-UUID strategy). Status /
    project_type vocabularies are identical across sub and CRM (§1.7), so the
    string values map straight onto the CRM enums.
    """
    from app.services.crm_portal import resolve_crm_subscriber_id

    subscriber_external_ref = None
    if project.subscriber_id:
        subscriber_external_ref = resolve_crm_subscriber_id(
            db, str(project.subscriber_id)
        )
    return {
        "id": str(project.id),
        "name": project.name,
        "status": project.status or "open",
        "project_type": project.project_type,
        "customer_address": project.customer_address,
        "region": project.region,
        "subscriber_external_ref": subscriber_external_ref,
        "source": "sub_relay",
    }


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def push_project_stub(db: Session, project_id: str) -> str:
    """Relay a single project stub to CRM. Returns an outcome key.

    Defensively re-checks the flag and vendor-relevance so the task is safe to
    run directly / on retry even if state changed since enqueue. Lets
    ``CRMClientError`` propagate so the Celery task retries (circuit / transient).
    """
    parsed = coerce_uuid(project_id)
    project = db.get(Project, parsed) if parsed else None
    if project is None:
        return "missing"
    if not relay_enabled(db):
        return "disabled"
    if not is_vendor_relevant(project):
        return "not_vendor_relevant"

    secret = settings.crm_webhook_secret
    if not secret:
        logger.warning("Vendor project relay: no CRM webhook secret configured")
        return "no_secret"

    payload = build_relay_payload(db, project)
    # Sign the exact bytes we send: the CRM verifies HMAC over the raw body, so
    # serialize once and post that buffer (not json=, which would re-encode).
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = _sign(secret, body)
    resp = get_crm_client().post_signed_webhook(
        RELAY_WEBHOOK_PATH, body=body, signature=signature
    )
    if resp.status_code == 200:
        logger.debug("Vendor project relay OK for %s", project.id)
        return "relayed"
    logger.warning(
        "Vendor project relay failed for %s: %d %s",
        project.id,
        resp.status_code,
        resp.text[:200],
    )
    return "failed"


def enqueue_project_relay(project_id: str | UUID, *, source: str) -> None:
    """Queue a relay push off the request thread; never raises into the caller."""
    if not settings.crm_base_url:
        return
    try:
        from app.services.queue_adapter import enqueue_task
        from app.tasks.vendor_project_relay import relay_project_stub_to_crm

        enqueue_task(relay_project_stub_to_crm, args=[str(project_id)], source=source)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Vendor project relay enqueue failed for %s: %s", project_id, exc
        )
