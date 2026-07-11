from __future__ import annotations

from uuid import uuid4

from app.models.dispatch import TechnicianProfile
from app.models.field_asset import FieldAsset, FieldAssetCustody
from app.models.field_material import FieldInventoryItem
from app.models.network import CPEDevice, DeviceStatus, DeviceType, OLTDevice, OntUnit
from app.models.network_monitoring import NetworkDevice
from app.models.router_management import Router
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.services.asset_inventory import AssetCatalogFilters, asset_inventory


def _system_user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Field",
        last_name="Tech",
        display_name="Field Tech",
        email=f"field-tech-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _technician(db_session, user: SystemUser) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        title="Installer",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Asset",
        last_name="Customer",
        email=f"asset-customer-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def test_asset_catalog_unifies_material_devices_and_field_fleet(db_session):
    subscriber = _subscriber(db_session)
    item = FieldInventoryItem(sku="DROP-100", name="Drop cable", unit="m")
    field_asset = FieldAsset(
        asset_tag="METER-001",
        asset_type="test_equipment",
        name="Optical power meter",
        status="issued",
        vendor="EXFO",
        serial_number="PM-001",
    )
    ont = OntUnit(serial_number="ONT-ASSET-001", vendor="Huawei", model="HG8546M")
    cpe = CPEDevice(
        subscriber_id=subscriber.id,
        device_type=DeviceType.router,
        status=DeviceStatus.active,
        serial_number="CPE-ASSET-001",
        vendor="MikroTik",
        model="hAP",
    )
    olt = OLTDevice(name="Garki OLT", hostname="olt-garki", mgmt_ip="10.10.0.2")
    network_device = NetworkDevice(name="Garki AP", hostname="ap-garki")
    router = Router(
        name="Core Router",
        hostname="core-router",
        management_ip="10.0.0.1",
        rest_api_username="admin",
        rest_api_password="secret",
    )
    db_session.add_all([item, field_asset, ont, cpe, olt, network_device, router])
    db_session.commit()

    result = asset_inventory.list_catalog(
        db_session, AssetCatalogFilters(limit=50, offset=0)
    )

    sources = {row["source"] for row in result["items"]}
    assert {
        "field_inventory",
        "field_asset",
        "ont",
        "cpe",
        "olt",
        "network_device",
        "router",
    }.issubset(sources)
    assert result["summary"]["field_asset"] == 1
    assert result["summary"]["total"] >= 7


def test_asset_catalog_tracks_field_technician_custody(db_session):
    user = _system_user(db_session)
    technician = _technician(db_session, user)
    field_asset = FieldAsset(
        asset_tag="VAN-001",
        asset_type="vehicle",
        name="Field van",
        status="issued",
        registration_number="ABC-123",
    )
    db_session.add(field_asset)
    db_session.flush()
    custody = FieldAssetCustody(
        asset_source="field_asset",
        asset_id=field_asset.id,
        field_asset_id=field_asset.id,
        technician_id=technician.id,
        system_user_id=user.id,
        status="issued",
    )
    db_session.add(custody)
    db_session.commit()

    result = asset_inventory.list_catalog(
        db_session,
        AssetCatalogFilters(
            assigned_to_technician_id=str(technician.id),
            limit=50,
            offset=0,
        ),
    )

    assert result["count"] == 1
    item = result["items"][0]
    assert item["source"] == "field_asset"
    assert item["asset_type"] == "vehicle"
    assert item["assigned_technician_id"] == technician.id
    assert item["assigned_system_user_id"] == user.id
    assert item["assigned_to"] == "Field Tech"


def test_asset_catalog_filters_customer_cpe_by_subscriber(db_session):
    subscriber = _subscriber(db_session)
    other = _subscriber(db_session)
    db_session.add_all(
        [
            CPEDevice(
                subscriber_id=subscriber.id,
                serial_number="CPE-OWNED",
                status=DeviceStatus.active,
            ),
            CPEDevice(
                subscriber_id=other.id,
                serial_number="CPE-OTHER",
                status=DeviceStatus.active,
            ),
        ]
    )
    db_session.commit()

    result = asset_inventory.list_catalog(
        db_session,
        AssetCatalogFilters(
            source="cpe",
            subscriber_id=str(subscriber.id),
            limit=50,
            offset=0,
        ),
    )

    assert result["count"] == 1
    assert result["items"][0]["serial_number"] == "CPE-OWNED"
