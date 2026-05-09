"""Tests for Huawei OLT profile resolution helpers."""

from datetime import UTC, datetime

from sqlalchemy import select

from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    OfferStatus,
    PlanCategory,
    PriceBasis,
    ServiceType,
)
from app.models.network import (
    OLTDevice,
    OltLineProfile,
    OltOntRegistration,
    OltOnuTypeProfileMapping,
    OltProfileBundle,
    OltServiceProfile,
    OntUnit,
)
from app.services.network.olt_profile_resolution import (
    OntCapabilityCounts,
    ServiceProfileDetail,
    choose_service_profile,
    resolve_authorization_profiles_from_import,
)
from app.services.network.olt_state_import import (
    _import_profile_mappings,
    import_olt_state_from_dump,
)


def test_choose_service_profile_prefers_model_name_over_generic_count_match():
    profiles = [
        ServiceProfileDetail(
            profile_id=40,
            name="ONU-type-eth-4-pots-2-catv-0",
            ethernet_ports=4,
            voip_ports=2,
            catv_ports=0,
            binding_count=120,
        ),
        ServiceProfileDetail(
            profile_id=41,
            name="EG8145V5",
            ethernet_ports=4,
            voip_ports=2,
            catv_ports=0,
            binding_count=20,
        ),
    ]

    selected = choose_service_profile(
        profiles,
        capability=OntCapabilityCounts(ethernet_ports=4, voip_ports=2, catv_ports=0),
        model="EG8145V5",
    )

    assert selected is not None
    assert selected.profile_id == 41


def test_choose_service_profile_uses_capability_when_model_is_unknown():
    profiles = [
        ServiceProfileDetail(
            profile_id=40,
            name="ONU-type-eth-4-pots-2-catv-0",
            ethernet_ports=4,
            voip_ports=2,
            catv_ports=0,
            binding_count=20,
        ),
        ServiceProfileDetail(
            profile_id=45,
            name="ONU-type-eth-4-pots-1-catv-1",
            ethernet_ports=4,
            voip_ports=1,
            catv_ports=1,
            binding_count=1,
        ),
    ]

    selected = choose_service_profile(
        profiles,
        capability=OntCapabilityCounts(ethernet_ports=4, voip_ports=1, catv_ports=1),
    )

    assert selected is not None
    assert selected.profile_id == 45


def test_resolve_authorization_profiles_requires_imported_mapping(db_session):
    olt = OLTDevice(name="Imported Mapping OLT", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    imported_at = datetime.now(UTC)
    db_session.add_all(
        [
            OltLineProfile(
                olt_id=olt.id,
                profile_id=40,
                name="SMARTOLT_FLEXIBLE_GPON",
                last_imported_at=imported_at,
            ),
            OltServiceProfile(
                olt_id=olt.id,
                profile_id=41,
                name="EG8145V5",
                last_imported_at=imported_at,
            ),
        ]
    )
    db_session.flush()
    db_session.add(
        OltOnuTypeProfileMapping(
            olt_id=olt.id,
            equipment_id="EG8145V5",
            line_profile_id=40,
            service_profile_id=41,
            source_registration_count=10,
            last_imported_at=imported_at,
        )
    )
    db_session.flush()

    ok, message, resolved = resolve_authorization_profiles_from_import(
        db_session,
        olt,
        equipment_id="EG8145V5",
    )

    assert ok is True
    assert "Resolved imported OLT mapping" in message
    assert resolved is not None
    assert resolved.line_profile_id == 40
    assert resolved.service_profile_id == 41


def test_resolve_authorization_profiles_prefers_offer_bundle(db_session):
    olt = OLTDevice(name="Bundle Mapping OLT", vendor="Huawei")
    offer = CatalogOffer(
        name="Bundle Fiber 100",
        code="BF100",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        plan_category=PlanCategory.internet,
        status=OfferStatus.active,
        is_active=True,
        speed_download_mbps=100,
        speed_upload_mbps=50,
    )
    db_session.add_all([olt, offer])
    db_session.flush()
    db_session.add_all(
        [
            OltLineProfile(olt_id=olt.id, profile_id=40, name="LEGACY_LINE"),
            OltServiceProfile(olt_id=olt.id, profile_id=41, name="LEGACY_SERVICE"),
        ]
    )
    db_session.flush()
    db_session.add_all(
        [
            OltOnuTypeProfileMapping(
                olt_id=olt.id,
                equipment_id="EG8145V5",
                line_profile_id=40,
                service_profile_id=41,
            ),
            OltProfileBundle(
                olt_id=olt.id,
                offer_id=offer.id,
                name=offer.name,
                checksum="0" * 64,
                vlan_id=203,
                download_kbps=100_000,
                upload_kbps=50_000,
                dba_profile_id=100,
                download_traffic_table_id=101,
                upload_traffic_table_id=102,
                line_profile_id=150,
                service_profile_id=151,
                gem_id=1,
                tcont_id=1,
                command_plan={"groups": []},
                drift_status="applied",
                is_active=True,
            ),
        ]
    )
    db_session.flush()

    ok, message, resolved = resolve_authorization_profiles_from_import(
        db_session,
        olt,
        equipment_id="EG8145V5",
        offer_id=offer.id,
    )

    assert ok is True
    assert "Resolved OLT profile bundle" in message
    assert resolved is not None
    assert resolved.line_profile_id == 150
    assert resolved.service_profile_id == 151


def test_resolve_authorization_profiles_fails_without_imported_mapping(db_session):
    olt = OLTDevice(name="Missing Mapping OLT", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()

    ok, message, resolved = resolve_authorization_profiles_from_import(
        db_session,
        olt,
        equipment_id="HG8546M",
    )

    assert ok is False
    assert resolved is None
    assert "No imported profile mapping" in message


def test_import_profile_mappings_skips_ambiguous_equipment_without_guessing(
    db_session,
):
    olt = OLTDevice(name="Ambiguous Imported Mapping OLT", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    imported_at = datetime.now(UTC)
    db_session.add_all(
        [
            OltLineProfile(
                olt_id=olt.id,
                profile_id=40,
                name="line-40",
                last_imported_at=imported_at,
            ),
            OltServiceProfile(
                olt_id=olt.id,
                profile_id=41,
                name="EG8145V5",
                last_imported_at=imported_at,
            ),
            OltServiceProfile(
                olt_id=olt.id,
                profile_id=42,
                name="EG8145V5-alt",
                last_imported_at=imported_at,
            ),
        ]
    )
    db_session.flush()
    db_session.add_all(
        [
            OltOntRegistration(
                olt_id=olt.id,
                fsp="0/1/0",
                ont_id_on_olt=1,
                serial_number="4857544311111111",
                equipment_id="EG8145V5",
                line_profile_id=40,
                service_profile_id=41,
                is_active=True,
                last_imported_at=imported_at,
            ),
            OltOntRegistration(
                olt_id=olt.id,
                fsp="0/1/0",
                ont_id_on_olt=2,
                serial_number="4857544322222222",
                equipment_id="EG8145V5",
                line_profile_id=40,
                service_profile_id=42,
                is_active=True,
                last_imported_at=imported_at,
            ),
        ]
    )
    db_session.flush()
    warnings: list[str] = []

    count = _import_profile_mappings(db_session, olt, imported_at, warnings)

    assert count == 0
    assert warnings
    assert "Ambiguous imported profile mapping" in warnings[0]


def test_import_olt_state_from_dump_imports_profiles_registrations_and_mappings(
    db_session,
    tmp_path,
):
    olt = OLTDevice(name="Dump Import OLT", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    (tmp_path / "10_ont_lineprofile_all.txt").write_text(
        """
$ display ont-lineprofile gpon all
  -----------------------------------------------------------------------------
  Profile-ID  Profile-name                                Binding times
  -----------------------------------------------------------------------------
  40          SMARTOLT_FLEXIBLE_GPON                      2
  -----------------------------------------------------------------------------
  Total: 1
""",
        encoding="utf-8",
    )
    (tmp_path / "11_ont_srvprofile_all.txt").write_text(
        """
$ display ont-srvprofile gpon all
  -----------------------------------------------------------------------------
  Profile-ID  Profile-name                                Binding times
  -----------------------------------------------------------------------------
  40          ONU-type-eth-4-pots-2-catv-0                1
  41          EG8145V5                                    1
  -----------------------------------------------------------------------------
  Total: 2
""",
        encoding="utf-8",
    )
    (tmp_path / "99_running_config.txt").write_text(
        """
interface gpon 0/1
 ont add 0 1 sn-auth "4857544311111111" omci ont-lineprofile-id 40
ont-srvprofile-id 41 desc "Customer A"
 ont ipconfig 0 1 static ip-address 172.16.202.10 mask 255.255.255.0 vlan 201
 ont tr069-server-config 0 1 profile-id 2
 ont add 0 2 sn-auth "4857544322222222" omci ont-lineprofile-id 40
ont-srvprofile-id 40 desc "Customer B"
 ont tr069-server-config 0 2 profile-id 2
quit
""",
        encoding="utf-8",
    )

    result = import_olt_state_from_dump(db_session, str(olt.id), tmp_path)

    assert result.success is True
    assert result.line_profiles == 1
    assert result.service_profiles == 2
    assert result.ont_registrations == 2
    assert result.profile_mappings == 1
    mapping = db_session.scalars(
        select(OltOnuTypeProfileMapping).where(
            OltOnuTypeProfileMapping.olt_id == olt.id
        )
    ).one()
    assert mapping.equipment_id == "EG8145V5"
    assert mapping.line_profile_id == 40
    assert mapping.service_profile_id == 41


def test_import_olt_state_from_dump_preserves_ascii_registration_serial(
    db_session,
    tmp_path,
):
    olt = OLTDevice(name="Dump ASCII Serial OLT", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    (tmp_path / "10_ont_lineprofile_all.txt").write_text(
        """
$ display ont-lineprofile gpon all
  -----------------------------------------------------------------------------
  Profile-ID  Profile-name                                Binding times
  -----------------------------------------------------------------------------
  40          SMARTOLT_FLEXIBLE_GPON                      1
  -----------------------------------------------------------------------------
  Total: 1
""",
        encoding="utf-8",
    )
    (tmp_path / "11_ont_srvprofile_all.txt").write_text(
        """
$ display ont-srvprofile gpon all
  -----------------------------------------------------------------------------
  Profile-ID  Profile-name                                Binding times
  -----------------------------------------------------------------------------
  41          EG8145V5                                    1
  -----------------------------------------------------------------------------
  Total: 1
""",
        encoding="utf-8",
    )
    (tmp_path / "99_running_config.txt").write_text(
        """
interface gpon 0/2
 ont add 9 12 sn-auth "HWTC600AC29C" omci ont-lineprofile-id 40
ont-srvprofile-id 41 desc "ASCII serial"
quit
""",
        encoding="utf-8",
    )

    result = import_olt_state_from_dump(db_session, str(olt.id), tmp_path)

    assert result.success is True
    registration = db_session.scalars(
        select(OltOntRegistration).where(OltOntRegistration.olt_id == olt.id)
    ).one()
    assert registration.serial_number == "HWTC600AC29C"


def test_import_olt_state_from_dump_canonicalizes_hex_registration_serial(
    db_session,
    tmp_path,
):
    olt = OLTDevice(name="Dump Hex Serial OLT", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    (tmp_path / "10_ont_lineprofile_all.txt").write_text(
        """
$ display ont-lineprofile gpon all
  -----------------------------------------------------------------------------
  Profile-ID  Profile-name                                Binding times
  -----------------------------------------------------------------------------
  40          SMARTOLT_FLEXIBLE_GPON                      1
  -----------------------------------------------------------------------------
  Total: 1
""",
        encoding="utf-8",
    )
    (tmp_path / "11_ont_srvprofile_all.txt").write_text(
        """
$ display ont-srvprofile gpon all
  -----------------------------------------------------------------------------
  Profile-ID  Profile-name                                Binding times
  -----------------------------------------------------------------------------
  41          EG8145V5                                    1
  -----------------------------------------------------------------------------
  Total: 1
""",
        encoding="utf-8",
    )
    (tmp_path / "99_running_config.txt").write_text(
        """
interface gpon 0/2
 ont add 9 12 sn-auth "48575443600AC29C" omci ont-lineprofile-id 40
ont-srvprofile-id 41 desc "HEX serial"
quit
""",
        encoding="utf-8",
    )

    result = import_olt_state_from_dump(db_session, str(olt.id), tmp_path)

    assert result.success is True
    registration = db_session.scalars(
        select(OltOntRegistration).where(OltOntRegistration.olt_id == olt.id)
    ).one()
    assert registration.serial_number == "HWTC600AC29C"


def test_import_olt_state_from_dump_maps_inventory_model_to_imported_registration(
    db_session,
    tmp_path,
):
    olt = OLTDevice(name="Inventory Backed Import OLT", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    db_session.add(
        OntUnit(
            serial_number="HWTC7D4638C3",
            vendor_serial_number="485754437D4638C3",
            model="HG8145V5",
            olt_device_id=olt.id,
            board="0/2",
            port="10",
            external_id="0/2/10.12",
            is_active=True,
        )
    )
    db_session.flush()
    (tmp_path / "10_ont_lineprofile_all.txt").write_text(
        """
$ display ont-lineprofile gpon all
  -----------------------------------------------------------------------------
  Profile-ID  Profile-name                                Binding times
  -----------------------------------------------------------------------------
  40          SMARTOLT_FLEXIBLE_GPON                      1
  -----------------------------------------------------------------------------
  Total: 1
""",
        encoding="utf-8",
    )
    (tmp_path / "11_ont_srvprofile_all.txt").write_text(
        """
$ display ont-srvprofile gpon all
  -----------------------------------------------------------------------------
  Profile-ID  Profile-name                                Binding times
  -----------------------------------------------------------------------------
  9           EG8145V5                                    1
  -----------------------------------------------------------------------------
  Total: 1
""",
        encoding="utf-8",
    )
    (tmp_path / "99_running_config.txt").write_text(
        """
interface gpon 0/2
 ont add 10 12 sn-auth "%#%#encrypted%#%#" omci
ont-lineprofile-id 40 ont-srvprofile-id 9 desc "OPTIMA_LTD"
quit
""",
        encoding="utf-8",
    )

    result = import_olt_state_from_dump(db_session, str(olt.id), tmp_path)

    assert result.success is True
    mapping = db_session.scalars(
        select(OltOnuTypeProfileMapping).where(
            OltOnuTypeProfileMapping.olt_id == olt.id,
            OltOnuTypeProfileMapping.equipment_id == "HG8145V5",
        )
    ).one()
    assert mapping.line_profile_id == 40
    assert mapping.service_profile_id == 9
