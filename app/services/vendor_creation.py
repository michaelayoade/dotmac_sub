"""Atomic vendor creation and ERP supplier delivery intent."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.services import vendor_admin
from app.services.dotmac_erp.vendor_supplier_sync import enqueue_vendor
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

_DEFINITION = OwnerCommandDefinition(
    owner="operations.vendor_creation",
    concern="native vendor creation and ERP supplier delivery intent",
    name="create_vendor",
)


@dataclass(frozen=True, slots=True)
class CreateVendorCommand:
    context: CommandContext
    name: str
    code: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    license_number: str | None = None
    service_area: str | None = None
    notes: str | None = None
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class CreateVendorOutcome:
    vendor_id: UUID
    erp_event_id: UUID


def create_vendor(db: Session, command: CreateVendorCommand) -> CreateVendorOutcome:
    def stage() -> CreateVendorOutcome:
        vendor = vendor_admin.create(
            db,
            name=command.name,
            code=command.code,
            contact_name=command.contact_name,
            contact_email=command.contact_email,
            contact_phone=command.contact_phone,
            license_number=command.license_number,
            service_area=command.service_area,
            notes=command.notes,
            is_active=command.is_active,
        )
        event = enqueue_vendor(db, vendor)
        emit_event(
            db,
            EventType.vendor_created,
            {"vendor_id": str(vendor.id)},
            record_only=True,
        )
        return CreateVendorOutcome(vendor_id=vendor.id, erp_event_id=event.id)

    return execute_owner_command(
        db, definition=_DEFINITION, context=command.context, operation=stage
    )
