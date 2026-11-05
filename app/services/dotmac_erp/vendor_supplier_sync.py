"""Vendor identity delivery to the ERP supplier owner."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.models.field_erp_sync import FieldErpSyncEvent, FieldErpSyncFlow
from app.models.vendor_routes import Vendor
from app.services.dotmac_erp import outbox


class SupplierCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: int = 1
    source_vendor_id: UUID
    name: str = Field(min_length=1, max_length=160)
    code: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    is_active: bool


class SupplierCreateResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_vendor_id: UUID
    supplier_id: UUID
    supplier_code: str
    created: bool


def enqueue_vendor(db: Session, vendor: Vendor) -> FieldErpSyncEvent:
    payload = SupplierCreatePayload(
        source_vendor_id=vendor.id,
        name=vendor.name,
        code=vendor.code,
        contact_name=vendor.contact_name,
        contact_email=vendor.contact_email,
        contact_phone=vendor.contact_phone,
        is_active=vendor.is_active,
    )
    return outbox.enqueue(
        db,
        flow=FieldErpSyncFlow.vendor_supplier,
        entity_type="vendor",
        entity_id=vendor.id,
        idempotency_key=f"vendor-supplier:{vendor.id}",
        payload=payload.model_dump(mode="json"),
        isolate=False,
    )


def apply_erp_response(db: Session, event: FieldErpSyncEvent) -> None:
    if event.status != "accepted":
        return
    response = SupplierCreateResponse.model_validate(event.erp_response)
    if response.source_vendor_id != event.entity_id:
        raise ValueError("ERP supplier source identity mismatch")
    vendor = db.get(Vendor, event.entity_id)
    if vendor is None:
        raise ValueError("Vendor for accepted ERP supplier is missing")
    reference = str(response.supplier_id)
    if vendor.supplier_reference and (
        vendor.supplier_system != "dotmac_erp" or vendor.supplier_reference != reference
    ):
        raise ValueError("Vendor has a conflicting supplier binding")
    vendor.supplier_system = "dotmac_erp"
    vendor.supplier_reference = reference
    db.flush()


def repair_accepted(db: Session, *, limit: int = 100) -> int:
    repaired = 0
    for event in outbox.unlinked_delivered_events(
        db, flow=FieldErpSyncFlow.vendor_supplier, limit=limit
    ):
        if event.status == "accepted":
            vendor = db.get(Vendor, event.entity_id)
            if vendor is not None and not vendor.supplier_reference:
                apply_erp_response(db, event)
                repaired += 1
    return repaired
