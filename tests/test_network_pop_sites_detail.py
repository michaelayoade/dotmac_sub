"""Tests for POP site detail data enrichment (hardware + customer services)."""

import uuid

from starlette.datastructures import FormData

from app.models.catalog import NasVendor, SubscriptionStatus
from app.models.network import NetworkZone
from app.models.stored_file import StoredFile
from app.models.subscriber import Organization, Reseller
from app.models.subscriber import Address
from app.schemas.catalog import NasDeviceCreate, SubscriptionCreate
from app.services import catalog as catalog_service
from app.services import nas as nas_service
from app.services import web_network_pop_sites as pop_sites_service


def test_pop_site_detail_includes_hardware_and_customer_services(
    db_session,
    pop_site,
    network_device,
    subscriber,
    catalog_offer,
):
    nas = nas_service.NasDevices.create(
        db_session,
        NasDeviceCreate(
            name="POP NAS",
            vendor=NasVendor.mikrotik,
            ip_address="10.10.10.1",
            management_ip="10.10.10.1",
            pop_site_id=pop_site.id,
        ),
    )

    catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
            provisioning_nas_device_id=nas.id,
            status=SubscriptionStatus.active,
            service_description="Business Fiber",
            login="cust-login-1",
            ipv4_address="100.64.1.10",
        ),
    )

    payload = pop_sites_service.detail_page_data(db_session, str(pop_site.id))
    assert payload is not None
    assert payload["service_impact_count"] == 1
    assert len(payload["customer_services"]) == 1
    assert any(device["device_type"] == "Core Device" for device in payload["hardware_devices"])
    assert any(device["device_type"] == "NAS Router" for device in payload["hardware_devices"])


def test_pop_site_detail_includes_map_markers_from_service_addresses(
    db_session,
    pop_site,
    subscriber,
    catalog_offer,
):
    nas = nas_service.NasDevices.create(
        db_session,
        NasDeviceCreate(
            name="POP NAS 2",
            vendor=NasVendor.mikrotik,
            ip_address="10.20.20.1",
            management_ip="10.20.20.1",
            pop_site_id=pop_site.id,
        ),
    )
    address = Address(
        subscriber_id=subscriber.id,
        address_line1="123 Fiber Street",
        city="Abuja",
        latitude=9.082,
        longitude=8.6753,
    )
    db_session.add(address)
    db_session.commit()
    db_session.refresh(address)

    catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber.id,
            offer_id=catalog_offer.id,
            provisioning_nas_device_id=nas.id,
            service_address_id=address.id,
            status=SubscriptionStatus.active,
            service_description="Residential Fiber",
        ),
    )

    payload = pop_sites_service.detail_page_data(db_session, str(pop_site.id))
    assert payload is not None
    markers = payload["map_markers"]
    assert any(marker["type"] == "service" for marker in markers)


def test_pop_site_detail_includes_gallery_and_documents(db_session, pop_site):
    photo = StoredFile(
        entity_type="pop_site_photo",
        entity_id=str(pop_site.id),
        original_filename="tower.jpg",
        storage_key_or_relative_path="branding/public/pop_site_photo/x/tower.jpg",
        file_size=1234,
        content_type="image/jpeg",
        storage_provider="s3",
    )
    doc = StoredFile(
        entity_type="pop_site_document_survey",
        entity_id=str(pop_site.id),
        original_filename="survey.pdf",
        storage_key_or_relative_path="attachments/public/pop_site_document_survey/x/survey.pdf",
        file_size=4321,
        content_type="application/pdf",
        storage_provider="s3",
    )
    db_session.add_all([photo, doc])
    db_session.commit()

    payload = pop_sites_service.detail_page_data(db_session, str(pop_site.id))
    assert payload is not None
    assert len(payload["photo_files"]) == 1
    assert len(payload["documents"]) == 1
    assert payload["documents"][0]["category"] == "survey"
    assert payload["documents"][0]["category_label"] == "Site Survey"


def test_pop_site_contact_lifecycle_helpers(db_session, pop_site):
    contact = pop_sites_service.create_contact(
        db_session,
        pop_site_id=str(pop_site.id),
        name="John Ops",
        role="Site Manager",
        phone="+2340000000",
        email="ops@example.com",
        notes="Escalation",
        is_primary=True,
    )
    payload = pop_sites_service.detail_page_data(db_session, str(pop_site.id))
    assert payload is not None
    assert len(payload["contacts"]) == 1
    assert payload["contacts"][0].name == "John Ops"
    assert payload["contacts"][0].is_primary is True

    deleted = pop_sites_service.delete_contact(
        db_session,
        pop_site_id=str(pop_site.id),
        contact_id=str(contact.id),
    )
    assert deleted is True
    payload = pop_sites_service.detail_page_data(db_session, str(pop_site.id))
    assert payload is not None
    assert len(payload["contacts"]) == 0


def test_resolve_site_relationships_assigns_zone_org_and_partner(db_session):
    zone = NetworkZone(name="Abuja Core", is_active=True)
    organization = Organization(name="Acme Fiber")
    reseller = Reseller(name="Metro Partner", is_active=True)
    db_session.add_all([zone, organization, reseller])
    db_session.commit()

    values = {
        "name": "POP Main",
        "zone_id_raw": str(zone.id),
        "organization_id_raw": str(organization.id),
        "reseller_id_raw": str(reseller.id),
    }

    normalized, error = pop_sites_service.resolve_site_relationships(db_session, values)
    assert error is None
    assert normalized is not None
    assert normalized["zone_id"] == zone.id
    assert normalized["organization_id"] == organization.id
    assert normalized["reseller_id"] == reseller.id


def test_resolve_site_relationships_rejects_unknown_ids(db_session):
    values = {
        "name": "POP Main",
        "zone_id_raw": str(uuid.uuid4()),
        "organization_id_raw": "",
        "reseller_id_raw": "",
    }
    normalized, error = pop_sites_service.resolve_site_relationships(db_session, values)
    assert normalized is None
    assert error == "Selected location reference was not found."


def test_parse_mast_form_supports_add_mast_toggle():
    form = FormData(
        {
            "add_mast": "true",
            "mast_name": "Mast 1",
            "mast_latitude": "",
            "mast_longitude": "",
            "mast_is_active": "true",
        }
    )
    enabled, payload, error, defaults = pop_sites_service.parse_mast_form(form, 9.08, 8.67)
    assert enabled is True
    assert error is None
    assert payload is not None
    assert payload["latitude"] == 9.08
    assert payload["longitude"] == 8.67
    assert payload["is_active"] is True
    assert defaults["name"] == "Mast 1"
