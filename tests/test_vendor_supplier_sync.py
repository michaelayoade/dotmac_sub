"""Vendor supplier payload and accepted identity projection."""

from uuid import uuid4

import pytest

from app.models.field_erp_sync import (
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    SyncFlowOwner,
    SyncFlowOwnership,
)
from app.models.vendor_routes import Vendor
from app.services.dotmac_erp import outbox
from app.services.dotmac_erp.client import DotMacERPTransientError
from app.services.dotmac_erp.vendor_supplier_sync import (
    apply_erp_response,
    enqueue_vendor,
)
from app.services.owner_commands import CommandContext
from app.services.vendor_creation import CreateVendorCommand, create_vendor


def test_vendor_creation_stages_supplier_delivery_with_native_identity(db_session):
    command_id = uuid4()
    outcome = create_vendor(
        db_session,
        CreateVendorCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=f"user:{uuid4()}",
                scope="vendor:create",
                reason="test vendor creation",
            ),
            name="Kano Fibre Works",
            code="KFW",
            contact_email="amina@example.test",
        ),
    )

    vendor = db_session.get(Vendor, outcome.vendor_id)
    event = db_session.get(FieldErpSyncEvent, outcome.erp_event_id)
    assert vendor is not None
    assert event is not None
    assert event.entity_id == vendor.id
    assert event.payload["source_vendor_id"] == str(vendor.id)


def test_vendor_supplier_intent_is_idempotent_and_maps_contact(db_session):
    vendor = Vendor(
        name="Kano Fibre Works",
        code="KFW",
        contact_name="Amina",
        contact_email="amina@example.test",
        contact_phone="+2348000000000",
        is_active=True,
    )
    db_session.add(vendor)
    db_session.flush()

    first = enqueue_vendor(db_session, vendor)
    repeated = enqueue_vendor(db_session, vendor)

    assert first.id == repeated.id
    assert first.flow == FieldErpSyncFlow.vendor_supplier.value
    assert first.payload["source_vendor_id"] == str(vendor.id)
    assert first.payload["contact_email"] == "amina@example.test"


def test_accepted_supplier_reference_rejects_conflicting_identity(db_session):
    vendor = Vendor(name="Lagos Trenching", is_active=True)
    db_session.add(vendor)
    db_session.flush()
    event = FieldErpSyncEvent(
        flow=FieldErpSyncFlow.vendor_supplier.value,
        entity_type="vendor",
        entity_id=vendor.id,
        idempotency_key=f"vendor-supplier:{vendor.id}",
        payload={},
        status="accepted",
        attempts=1,
        erp_response={
            "source_vendor_id": str(vendor.id),
            "supplier_id": str(uuid4()),
            "supplier_code": "LT",
            "created": True,
        },
    )
    db_session.add(event)
    db_session.flush()

    apply_erp_response(db_session, event)
    assert vendor.supplier_system == "dotmac_erp"
    assert vendor.supplier_reference == event.erp_response["supplier_id"]

    event.erp_response = {**event.erp_response, "supplier_id": str(uuid4())}
    with pytest.raises(ValueError, match="conflicting supplier binding"):
        apply_erp_response(db_session, event)


def test_transient_supplier_delivery_retries_and_links_once(db_session):
    vendor = Vendor(name="Abuja Cables", code="AC", is_active=True)
    db_session.add(vendor)
    db_session.add(
        SyncFlowOwnership(
            flow=FieldErpSyncFlow.vendor_supplier.value,
            owner=SyncFlowOwner.sub.value,
        )
    )
    db_session.flush()
    event = enqueue_vendor(db_session, vendor)
    db_session.commit()
    supplier_id = uuid4()

    class Client:
        attempts = 0

        def post(self, path, payload, **_kwargs):
            assert path.endswith("/suppliers")
            assert payload["source_vendor_id"] == str(vendor.id)
            self.attempts += 1
            if self.attempts == 1:
                raise DotMacERPTransientError("ERP temporarily unavailable")
            return {
                "source_vendor_id": str(vendor.id),
                "supplier_id": str(supplier_id),
                "supplier_code": "AC",
                "created": True,
            }

    client = Client()
    first = outbox.deliver_pending(db_session, client=client)
    assert first.retried == 1
    assert event.status == "pending"

    second = outbox.deliver_pending(db_session, client=client)
    assert second.accepted == 1
    assert client.attempts == 2
    assert vendor.supplier_reference == str(supplier_id)
