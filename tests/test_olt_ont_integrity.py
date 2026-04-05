from fastapi import HTTPException

from app.schemas.network import OltCardCreate, OltShelfCreate, OLTDeviceCreate, PonPortCreate
from app.services import network as network_service


def test_manual_pon_port_create_rejects_card_from_other_olt(db_session):
    olt_a = network_service.olt_devices.create(
        db_session,
        OLTDeviceCreate(name="OLT A", hostname="olt-a.local"),
    )
    olt_b = network_service.olt_devices.create(
        db_session,
        OLTDeviceCreate(name="OLT B", hostname="olt-b.local"),
    )
    shelf_b = network_service.olt_shelves.create(
        db_session,
        OltShelfCreate(olt_id=olt_b.id, shelf_number=1),
    )
    card_b = network_service.olt_cards.create(
        db_session,
        OltCardCreate(shelf_id=shelf_b.id, slot_number=2),
    )

    try:
        network_service.pon_ports.create(
            db_session,
            PonPortCreate(card_id=card_b.id, port_number=1, olt_id=olt_a.id),
        )
    except HTTPException as exc:
        assert exc.status_code == 400
        assert exc.detail == "OLT card does not belong to the selected OLT"
    else:
        raise AssertionError("Expected HTTPException for mismatched OLT card")
