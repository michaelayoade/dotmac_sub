from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.models.catalog import AccessType, CatalogOffer, PriceBasis, ServiceType
from app.models.network import (
    OltConfigBackup,
    OltConfigBackupType,
    OLTDevice,
    OltLineProfile,
    OltProfileBundle,
    OltServicePort,
    OntConfigSnapshot,
    OntUnit,
)
from app.services.network import config_evidence
from app.services.network.config_evidence import build_olt_config_evidence


def test_ont_config_evidence_marks_matching_observations_in_sync(
    db_session, monkeypatch, tmp_path
):
    backup_root = tmp_path / "olt_backups"
    backup_dir = backup_root / "olt"
    backup_dir.mkdir(parents=True)
    monkeypatch.setenv("OLT_BACKUP_DIR", str(backup_root))

    olt = OLTDevice(name="Evidence OLT", mgmt_ip="192.0.2.10")
    ont = OntUnit(
        serial_number="HWTC348F8A84",
        olt_device=olt,
        board="0/1",
        port=7,
        external_id="0/1/7:5",
        is_active=True,
    )
    db_session.add_all([olt, ont])
    db_session.flush()

    config_text = (
        "interface gpon 0/1\n"
        ' ont add 7 5 sn-auth "48575443348F8A84" omci '
        "ont-lineprofile-id 40 ont-srvprofile-id 41\n"
        " quit\n"
        "service-port 10 vlan 203 gpon 0/1/7 ont 5 gemport 1 "
        "multi-service user-vlan 203 tag-transform translate\n"
    )
    padded_config = config_text + ("\n#" * 600)
    path = backup_dir / "running.txt"
    path.write_text(padded_config)
    db_session.add(
        OltConfigBackup(
            olt_device_id=olt.id,
            backup_type=OltConfigBackupType.manual,
            file_path="olt/running.txt",
            file_size_bytes=len(padded_config.encode()),
            file_hash=hashlib.sha256(padded_config.encode()).hexdigest(),
        )
    )
    db_session.add(
        OltServicePort(
            olt_device_id=olt.id,
            ont_unit_id=ont.id,
            port_index=10,
            fsp="0/1/7",
            ont_id_on_olt=5,
            vlan_id=203,
            gem_index=1,
            source="test",
        )
    )
    db_session.add(
        OntConfigSnapshot(
            ont_unit_id=ont.id,
            source="tr069",
            label="after provision",
            wifi={"SSID": "Dotmac-WiFi"},
            wan={"WAN Mode": "pppoe"},
        )
    )
    db_session.flush()
    monkeypatch.setattr(
        config_evidence,
        "resolve_effective_ont_config",
        lambda *_args, **_kwargs: {
            "values": {
                "wan_vlan": 203,
                "wan_gem_index": 1,
                "authorization_line_profile_id": 40,
                "authorization_service_profile_id": 41,
                "wifi_ssid": "Dotmac-WiFi",
                "wan_mode": "pppoe",
            }
        },
    )

    evidence = config_evidence.build_ont_config_evidence(db_session, ont.id)

    assert evidence["status"] == "in_sync"
    assert evidence["counts"]["in_sync"] == 6
    assert evidence["counts"]["drift"] == 0
    assert {source["label"]: source["status"] for source in evidence["sources"]}[
        "Latest OLT backup"
    ] == "ok"


def test_ont_config_evidence_reports_drift_and_unknown_when_evidence_differs(
    db_session, monkeypatch
):
    from app.models.network import OLTDevice, OltServicePort, OntUnit
    from app.services.network import config_evidence

    olt = OLTDevice(name="Drift OLT")
    ont = OntUnit(
        serial_number="DRIFT-ONT-001",
        olt_device=olt,
        board="0/1",
        port=7,
        external_id="0/1/7:5",
        is_active=True,
    )
    db_session.add_all([olt, ont])
    db_session.flush()
    db_session.add(
        OltServicePort(
            olt_device_id=olt.id,
            ont_unit_id=ont.id,
            port_index=10,
            fsp="0/1/7",
            ont_id_on_olt=5,
            vlan_id=204,
            gem_index=9,
            source="test",
        )
    )
    db_session.flush()
    monkeypatch.setattr(
        config_evidence,
        "resolve_effective_ont_config",
        lambda *_args, **_kwargs: {
            "values": {
                "wan_vlan": 203,
                "wan_gem_index": 1,
                "authorization_line_profile_id": 40,
                "authorization_service_profile_id": 41,
            }
        },
    )

    evidence = config_evidence.build_ont_config_evidence(db_session, ont.id)

    assert evidence["status"] == "drift"
    checks = {check["label"]: check for check in evidence["drift_checks"]}
    assert checks["WAN VLAN"]["status"] == "drift"
    assert checks["WAN GEM"]["status"] == "drift"
    assert checks["Line profile"]["status"] == "unknown"


def test_ont_config_evidence_rejects_tampered_snapshot(db_session, monkeypatch):
    ont = OntUnit(serial_number="TAMPERED-ONT-001", is_active=True)
    db_session.add(ont)
    db_session.flush()
    snapshot = OntConfigSnapshot(
        ont_unit_id=ont.id,
        schema_version=2,
        source="tr069",
        wifi={"SSID": "CustomerNet"},
        payload_checksum="0" * 64,
    )
    db_session.add(snapshot)
    db_session.flush()
    monkeypatch.setattr(
        config_evidence,
        "resolve_effective_ont_config",
        lambda *_args, **_kwargs: {
            "values": {"wifi_ssid": "CustomerNet", "wan_mode": None}
        },
    )

    evidence = config_evidence.build_ont_config_evidence(db_session, ont.id)

    assert evidence["status"] == "drift"
    sources = {source["label"]: source for source in evidence["sources"]}
    assert sources["ONT config snapshot"]["status"] == "drift"
    checks = {check["label"]: check for check in evidence["drift_checks"]}
    assert checks["Snapshot integrity"]["status"] == "drift"
    assert checks["WiFi SSID"]["status"] == "unknown"


def test_olt_config_evidence_summarizes_backups_imports_and_bundle_drift(db_session):
    olt = OLTDevice(name="OLT Evidence")
    offer = CatalogOffer(
        name="Fiber 50",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
    )
    db_session.add_all([olt, offer])
    db_session.flush()
    db_session.add_all(
        [
            OltConfigBackup(
                olt_device_id=olt.id,
                backup_type=OltConfigBackupType.auto,
                file_path="olt/backup.txt",
                file_size_bytes=2048,
                file_hash="abc",
                created_at=datetime.now(UTC),
            ),
            OltLineProfile(olt_id=olt.id, profile_id=40, name="LINE"),
            OltServicePort(
                olt_device_id=olt.id,
                port_index=10,
                fsp="0/1/7",
                ont_id_on_olt=5,
                vlan_id=203,
                gem_index=1,
                source="test",
            ),
            OltProfileBundle(
                olt_id=olt.id,
                offer_id=offer.id,
                name="DOTMAC_50M",
                checksum="checksum",
                vlan_id=203,
                download_kbps=50_000,
                upload_kbps=25_000,
                dba_profile_id=100,
                download_traffic_table_id=101,
                upload_traffic_table_id=102,
                line_profile_id=40,
                service_profile_id=41,
                gem_id=1,
                tcont_id=1,
                drift_status="drifted",
            ),
        ]
    )
    db_session.flush()

    evidence = build_olt_config_evidence(db_session, olt.id)

    assert evidence["status"] == "drift"
    assert evidence["counts"]["imported_profiles"] == 1
    assert evidence["counts"]["imported_service_ports"] == 1
    assert evidence["counts"]["bundle_drift"]["drifted"] == 1
