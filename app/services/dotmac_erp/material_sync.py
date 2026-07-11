"""Material-request (ISSUE) flow for the sub → DotMac ERP outbox (ERP re-home, PR 3).

Second money flow to move onto sub's ``field_erp_sync_events`` outbox. Ports
``dotmac_crm/app/services/dotmac_erp/material_request_sync.py`` onto sub's native
``FieldMaterialRequest`` (which PR 3 gives the ERP mirror fields
``erp_material_request_id`` / ``erp_material_status``). Structurally identical to
``expense_sync.py``.

Three responsibilities live here:

* **map + enqueue** — ``enqueue_material_request`` builds the ERP payload (a port
  of CRM's ``_map_material_request`` shape, ``request_type='ISSUE'``), computes
  the stable idempotency key ``mr-{id}-approve-v1``, and hands it to
  ``outbox.enqueue``. It does NOT deliver — the worker owns delivery, and the
  outbox refuses any flow sub does not own in ``sync_flow_ownership``.
* **write-back** — ``apply_erp_response`` runs on the outbox's accepted/rejected
  path and writes the ERP request id / status back onto the source row (the
  "dropped money link" mitigation from the review doc). Terminal ERP fulfillment
  flips the sub row to ``fulfilled`` (verbatim CRM parity).
* **reconcile** — ``refresh_material_request_statuses`` polls ERP for in-flight
  requests and refreshes the mirror fields (ports CRM's status-poll refresh).

INERT UNTIL CUTOVER: nothing here sends. The approve hook only enqueues when the
master flag ``dotmac_erp_sync_enabled`` is on, and delivery is additionally gated
per-flow by ``sync_flow_ownership.material_request`` (seeded ``crm``). Both must
flip at cutover before a single request reaches ERP.

SERIALS / WAREHOUSE GAP (see PR description): sub's ``FieldMaterialRequest`` has
no first-class serial-number or source-warehouse tracking, whereas CRM sources
``serial_numbers`` from ``item.serial_numbers`` and ``from_warehouse_code`` from
``mr.source_location``. Here both are read best-effort from the request/item
``metadata`` JSON when present and omitted otherwise — no serials-tracking
subsystem is invented. ERP's ``CRMMaterialRequestItemPayload.from_warehouse_code``
is a REQUIRED field, so a serialized/warehoused ISSUE cannot be delivered until
sub grows those (a separate inventory-decision item). This is safe today because
the flow is inert until cutover.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session, selectinload

from app.models.field_erp_sync import FieldErpSyncEvent, FieldErpSyncFlow
from app.models.field_material import FieldMaterialRequest, FieldMaterialRequestItem
from app.services.dotmac_erp import outbox
from app.services.dotmac_erp.client import DotMacERPClient, build_erp_client

logger = logging.getLogger(__name__)

ENTITY_TYPE = "field_material_request"

# ERP status pushed for an ISSUE material request (verbatim CRM parity: CRM sends
# ``MaterialRequestStatus.issued.value``).
_ERP_ISSUE_STATUS = "issued"

# The sub-side statuses a request can still change while ERP owns fulfillment;
# only these get polled for a status refresh.
_IN_FLIGHT_STATUSES = ("approved", "issued")

# ERP material statuses that map onto a terminal sub status. Ported from CRM's
# ``refresh_material_request_status`` (which flips to fulfilled on
# fulfilled/complete/completed). Anything else only refreshes the mirror field.
_ERP_TERMINAL_STATUS_MAP = {
    "fulfilled": "fulfilled",
    "complete": "fulfilled",
    "completed": "fulfilled",
}


# ---------------------------------------------------------------------------
# Mapping + idempotency key (port of CRM's _map_material_request)
# ---------------------------------------------------------------------------


def material_request_idempotency_key(request: FieldMaterialRequest) -> str:
    """Stable per-request key: ``mr-{id}-approve-v1``.

    Constant across re-approvals of the same request, so a re-enqueue returns the
    existing outbox row and a re-delivery is a no-op on the ERP side.
    """
    return f"mr-{request.id}-approve-v1"


def _requester_email(request: FieldMaterialRequest) -> str | None:
    """Resolve the requesting employee's email (ERP matches employees by email)."""
    user = request.requested_by_system_user
    email = (getattr(user, "email", None) or "").strip()
    return email or None


def _metadata(obj: object) -> dict:
    data = getattr(obj, "metadata_", None)
    return data if isinstance(data, dict) else {}


def _item_serial_numbers(item: FieldMaterialRequestItem) -> list[str]:
    """Best-effort serials for a line — read from item ``metadata`` (see gap note).

    Mirrors CRM's read of ``item.serial_numbers``. Sub has no first-class serials
    column, so serials only flow when a caller has stashed them on the item
    ``metadata`` JSON; absent, the line is sent without serials.
    """
    raw = _metadata(item).get("serial_numbers")
    if not isinstance(raw, list):
        return []
    return [str(serial).strip() for serial in raw if str(serial).strip()]


def _from_warehouse_code(request: FieldMaterialRequest) -> str | None:
    """Best-effort source warehouse — read from request ``metadata`` (see gap note).

    Mirrors CRM's ``mr.source_location.code``. Sub has no source-location model,
    so this comes from the request ``metadata`` JSON when a caller supplied it;
    absent, ``None`` (ERP requires it, so such a payload cannot deliver until sub
    grows warehouse tracking — a separate inventory-decision item).
    """
    meta = _metadata(request)
    code = meta.get("from_warehouse_code") or meta.get("warehouse_code")
    cleaned = str(code).strip() if code else ""
    return cleaned or None


def build_material_request_payload(request: FieldMaterialRequest) -> dict:
    """Map a ``FieldMaterialRequest`` to ERP's ``CRMMaterialRequestPayload`` shape.

    Mirrors CRM's ``_map_material_request``: ``omni_id`` is sub's request UUID,
    ``request_type='ISSUE'``, ``status='issued'``, each line carries
    ``item_code`` / ``quantity`` / ``uom`` / ``from_warehouse_code`` (and
    ``serial_numbers`` when known). ``requested_by_email`` lets ERP match the
    employee; ``ticket_crm_id`` comes off the work-order mirror (sub has no direct
    FK). See the module docstring for the serials/warehouse fidelity gap.
    """
    warehouse_code = _from_warehouse_code(request)

    item_rows: list[dict[str, object]] = []
    for item in request.items:
        inv_item = item.item
        row: dict[str, object] = {
            "item_code": (
                getattr(inv_item, "sku", None)
                or getattr(inv_item, "name", None)
                or str(item.item_id)
            ),
            "quantity": item.quantity,
            "uom": getattr(inv_item, "unit", None) or "PCS",
            "from_warehouse_code": warehouse_code,
        }
        serial_numbers = _item_serial_numbers(item)
        if serial_numbers:
            row["serial_numbers"] = serial_numbers
        item_rows.append(row)

    schedule_date = (
        (request.approved_at or request.submitted_at or request.created_at)
        .date()
        .isoformat()
    )

    mirror = request.work_order_mirror

    return {
        "omni_id": str(request.id),
        "request_type": "ISSUE",
        "status": _ERP_ISSUE_STATUS,
        "schedule_date": schedule_date,
        "requested_by_email": _requester_email(request),
        "ticket_crm_id": getattr(mirror, "crm_ticket_id", None),
        "remarks": request.notes or "",
        "items": item_rows,
    }


def material_request_eligibility_error(request: FieldMaterialRequest) -> str | None:
    """Return a reason string if the request is NOT eligible for ERP sync, else None.

    Ports CRM's ``_validate_material_request_for_sync`` onto sub: must be
    ``approved``, have at least one line, and carry a requester email (ERP needs
    it to match the employee). The source-warehouse requirement is relaxed vs CRM
    (sub has no source_location) — see the module docstring gap note.
    """
    if request.status != "approved":
        return (
            f"Material request {request.id} is in {request.status} status and "
            "cannot be synced"
        )
    if not request.items:
        return f"Material request {request.id} has no items — cannot sync to ERP"
    if not _requester_email(request):
        return "Requester has no email address; ERP needs it to match the employee"
    return None


# ---------------------------------------------------------------------------
# Enqueue (approve hook target)
# ---------------------------------------------------------------------------


def enqueue_material_request(
    db: Session, request: FieldMaterialRequest
) -> FieldErpSyncEvent | None:
    """Enqueue the material-request outbox intent for an approved request.

    Validates eligibility, builds the payload + stable key, and calls
    ``outbox.enqueue`` (idempotent on the key). Returns the outbox row, or ``None``
    when the request is not eligible (logged, not raised — an ineligible request
    must never break the approve transaction). Does NOT deliver.
    """
    reason = material_request_eligibility_error(request)
    if reason:
        logger.info(
            "material_sync: not enqueuing material request %s — %s", request.id, reason
        )
        return None

    payload = build_material_request_payload(request)
    return outbox.enqueue(
        db,
        flow=FieldErpSyncFlow.material_request,
        entity_type=ENTITY_TYPE,
        entity_id=request.id,
        idempotency_key=material_request_idempotency_key(request),
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Response write-back (outbox accepted/rejected path + status reconcile)
# ---------------------------------------------------------------------------


def _extract_request_id(response: dict | None) -> str | None:
    if not isinstance(response, dict):
        return None
    erp_id = (
        response.get("request_id")
        or response.get("material_request_id")
        or response.get("request_number")
    )
    return str(erp_id) if erp_id else None


def _extract_material_status(response: dict | None) -> str | None:
    if not isinstance(response, dict):
        return None
    raw = (
        response.get("material_status")
        or response.get("erp_material_status")
        or response.get("status")
    )
    if not raw:
        return None
    status = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    return status[:40] if status else None


def apply_material_response(
    request: FieldMaterialRequest, response: dict | None
) -> None:
    """Write an ERP material-request response back onto a ``FieldMaterialRequest``.

    Shared by the outbox accepted/rejected path and the status reconcile. Ports
    CRM's write-back: records request id / status and, when ERP reports terminal
    fulfillment, flips the sub row to ``fulfilled`` + stamps ``fulfilled_at``.
    Idempotent — safe to run on every poll.
    """
    if not isinstance(response, dict):
        return

    erp_id = _extract_request_id(response)
    material_status = _extract_material_status(response)

    if erp_id and not request.erp_material_request_id:
        request.erp_material_request_id = str(erp_id)[:120]
    if not material_status:
        return

    request.erp_material_status = material_status
    mapped = _ERP_TERMINAL_STATUS_MAP.get(material_status)
    if mapped == "fulfilled" and request.status in _IN_FLIGHT_STATUSES:
        request.status = "fulfilled"
        request.fulfilled_at = request.fulfilled_at or datetime.now(UTC)


def apply_erp_response(db: Session, event: FieldErpSyncEvent) -> None:
    """Outbox write-back hook: apply a delivered event's ERP response to its source.

    Called by ``outbox.deliver_pending`` after a 2xx classify, within the same
    transaction (the outbox commits the row). Loads the ``FieldMaterialRequest``
    the event pushed and applies ``apply_material_response``. Missing source rows
    are logged, not raised — the event still records its terminal outcome.
    """
    request = db.get(FieldMaterialRequest, event.entity_id)
    if request is None:
        logger.warning(
            "material_sync: outbox event %s has no FieldMaterialRequest %s to "
            "write ERP response back to",
            event.id,
            event.entity_id,
        )
        return
    apply_material_response(request, event.erp_response)


# ---------------------------------------------------------------------------
# Status reconcile (beat-driven, gated by dotmac_erp_sync_enabled)
# ---------------------------------------------------------------------------


def refresh_material_request_statuses(
    db: Session, *, client: DotMacERPClient | None = None, limit: int = 100
) -> dict:
    """Poll ERP for in-flight material requests and refresh their mirror fields.

    Selects synced (``erp_material_request_id`` set) requests still awaiting ERP
    fulfillment (``approved`` / ``issued``), polls
    ``get_material_request_status(request.id)`` for each, and applies the response
    via ``apply_material_response``. Ports CRM's material status refresh.
    Read-only against ERP; idempotent; safe to re-run.
    """
    limit = max(1, min(int(limit or 100), 200))
    pending = (
        db.query(FieldMaterialRequest)
        .options(
            selectinload(FieldMaterialRequest.items).selectinload(
                FieldMaterialRequestItem.item
            )
        )
        .filter(FieldMaterialRequest.is_active.is_(True))
        .filter(FieldMaterialRequest.erp_material_request_id.isnot(None))
        .filter(FieldMaterialRequest.status.in_(_IN_FLIGHT_STATUSES))
        .order_by(FieldMaterialRequest.updated_at.asc())
        .limit(limit)
        .all()
    )

    errors: list[str] = []
    result: dict[str, object] = {"processed": 0, "updated": 0, "errors": errors}
    if not pending:
        return result

    owned_client = client
    created_client = False
    if owned_client is None:
        owned_client = build_erp_client(db)
        created_client = True

    processed = 0
    updated = 0
    try:
        for request in pending:
            processed += 1
            try:
                response = owned_client.get_material_request_status(str(request.id))
            except Exception as exc:  # noqa: BLE001 — one bad row can't stall the batch
                db.rollback()
                errors.append(f"{request.id}: {exc}")
                logger.warning(
                    "material_sync: status refresh failed for %s: %s", request.id, exc
                )
                continue
            if not response:
                continue
            before = request.erp_material_status
            apply_material_response(request, response)
            if request.erp_material_status != before:
                updated += 1
            db.commit()
    finally:
        if created_client:
            owned_client.close()

    result["processed"] = processed
    result["updated"] = updated
    return result
