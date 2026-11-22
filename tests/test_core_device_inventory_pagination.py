"""Core-device pages count the full filtered scope rather than the row slice."""

from app.models.network_monitoring import DeviceRole, NetworkDevice
from app.services.web_network_core_devices_forms import list_page_data


def test_core_device_page_has_matching_total_and_stable_next_page(db_session):
    db_session.add_all(
        [
            NetworkDevice(name=f"Inventory router {index:02d}", role=DeviceRole.core)
            for index in range(12)
        ]
    )
    db_session.flush()

    first = list_page_data(
        db_session,
        role=None,
        status="all",
        page=1,
        per_page=10,
        search="Inventory router",
    )
    second = list_page_data(
        db_session,
        role=None,
        status="all",
        page=2,
        per_page=10,
        search="Inventory router",
    )

    assert first["stats"]["total"] == 12
    assert first["stats"]["core"] == 12
    assert len(first["devices"]) == 10
    assert first["pagination"]["has_next"] is True
    assert len(second["devices"]) == 2
