"""OLT/CPE class_facts now populated in the device projection."""

from app.models.network import OLTDevice
from app.services import web_network_core_devices_inventory as inventory


def test_collect_devices_olt_carries_software_and_pon_class_facts(db_session):
    db_session.add(
        OLTDevice(
            name="Facts OLT",
            mgmt_ip="203.0.113.44",
            software_version="R19.2",
            firmware_version="fw-7.1",
            supported_pon_types="gpon,xgspon",
        )
    )
    db_session.commit()

    olt_rows = [
        d
        for d in inventory.collect_devices(db_session)
        if d["type"] == "olt" and d["name"] == "Facts OLT"
    ]
    assert olt_rows, "seeded OLT was not projected"
    cf = olt_rows[0]["class_facts"]
    assert cf is not None
    assert cf["software_version"] == "R19.2"
    assert cf["firmware_version"] == "fw-7.1"
    assert cf["pon_types"] == "gpon,xgspon"
