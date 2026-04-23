from __future__ import annotations

from app.models.network import OntProvisioningProfile, OnuType, VendorModelCapability
from app.services import web_network_onu_types as service


def test_onu_type_create_persists_capability_map_and_default_bundle(db_session) -> None:
    capability = VendorModelCapability(
        vendor="Huawei",
        model="HG8245H",
        is_active=True,
    )
    bundle = OntProvisioningProfile(name="Huawei Default Bundle", is_active=True)
    db_session.add_all([capability, bundle])
    db_session.commit()

    values = {
        "name": "Huawei HG8245H",
        "pon_type": "gpon",
        "gpon_channel": "gpon",
        "ethernet_ports": 4,
        "wifi_ports": 2,
        "voip_ports": 1,
        "catv_ports": 0,
        "allow_custom_profiles": True,
        "capability": "bridging_routing",
        "vendor_model_capability_id": str(capability.id),
        "default_bundle_id": str(bundle.id),
        "supports_bundle_overrides": False,
        "notes": None,
    }

    assert service.validate_form(values, db_session) is None
    created = service.handle_create(db_session, values)

    db_session.refresh(created)
    assert isinstance(created, OnuType)
    assert created.vendor_model_capability_id == capability.id
    assert created.default_bundle_id == bundle.id
    assert created.supports_bundle_overrides is False
