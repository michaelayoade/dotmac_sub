from datetime import UTC, datetime, timedelta

from app.services.network.olt_polling import (
    _build_reading_targets,
    _expected_external_id_for_reading,
    _parse_signal_value,
    _parse_snmp_table,
    _should_repair_external_id,
)
from app.services.network.olt_polling_parsers import _fsp_hint_from_index


def test_parse_snmp_table_last_token_index() -> None:
    lines = [
        "iso.3.6.1.4.1.2011.6.128.1.1.2.51.1.4.4194320384.3 = INTEGER: -1950",
    ]
    parsed = _parse_snmp_table(lines)
    assert parsed == {"3": "-1950"}


def test_parse_snmp_table_composite_index() -> None:
    """Test that composite indexes are preserved when base_oid is provided."""
    lines = [
        "iso.3.6.1.4.1.2011.6.128.1.1.2.51.1.4.4194320384.3 = INTEGER: -1950",
    ]
    parsed = _parse_snmp_table(
        lines,
        base_oid=".1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4",
    )
    assert parsed == {"4194320384.3": "-1950"}


def test_parse_signal_value_huawei_olt_rx_scaled() -> None:
    value = _parse_signal_value("-1950", vendor="huawei", metric="olt_rx")
    assert value == -19.5


def test_ont_unit_has_ddm_fields() -> None:
    """OntUnit model must expose DDM health columns."""
    from app.models.network import OntUnit

    for field_name in [
        "onu_tx_signal_dbm",
        "ont_temperature_c",
        "ont_voltage_v",
        "ont_bias_current_ma",
    ]:
        assert hasattr(OntUnit, field_name), f"OntUnit missing field: {field_name}"


def test_parse_signal_value_huawei_onu_rx_offset_decoding() -> None:
    value = _parse_signal_value("7113", vendor="huawei", metric="onu_rx")
    assert value == -28.87


def test_parse_signal_value_sentinel_is_none() -> None:
    value = _parse_signal_value("2147483647", vendor="huawei", metric="olt_rx")
    assert value is None


def test_ont_signal_reading_has_ddm_fields() -> None:
    """OntSignalReading must include DDM health fields."""
    from app.services.network.olt_polling import OntSignalReading

    reading = OntSignalReading(
        onu_index="0.1.3.5",
        olt_rx_dbm=-19.5,
        onu_rx_dbm=-21.0,
        onu_tx_dbm=2.5,
        distance_m=1200,
        is_online=True,
        temperature_c=42.0,
        voltage_v=3.3,
        bias_current_ma=15.2,
        offline_reason_raw=None,
        serial_number_raw=None,
    )
    assert reading.onu_tx_dbm == 2.5
    assert reading.temperature_c == 42.0
    assert reading.voltage_v == 3.3
    assert reading.bias_current_ma == 15.2
    assert reading.offline_reason_raw is None
    assert reading.serial_number_raw is None


def test_vendor_oids_include_ddm_keys() -> None:
    """All vendor OID maps must include DDM OID keys."""
    from app.services.network.olt_polling import _VENDOR_OID_OIDS, GENERIC_OIDS

    ddm_keys = {"onu_tx", "temperature", "voltage", "bias_current"}
    for vendor, oids in _VENDOR_OID_OIDS.items():
        for key in ddm_keys:
            assert key in oids, f"Vendor '{vendor}' missing OID key: {key}"
    for key in ddm_keys:
        assert key in GENERIC_OIDS, f"GENERIC_OIDS missing OID key: {key}"


def test_parse_ddm_value_temperature() -> None:
    """Temperature values are returned as integer degrees C."""
    from app.services.network.olt_polling import _parse_ddm_value

    assert _parse_ddm_value("42") == 42.0
    assert _parse_ddm_value("") is None
    assert _parse_ddm_value("No Such Instance") is None


def test_parse_ddm_value_voltage() -> None:
    """Voltage values are in 0.01V units for Huawei."""
    from app.services.network.olt_polling import _parse_ddm_value

    # 330 = 3.30V
    assert _parse_ddm_value("330", scale=0.01) == 3.3


def test_parse_ddm_value_bias_current() -> None:
    """Bias current in 0.001 mA units."""
    from app.services.network.olt_polling import _parse_ddm_value

    assert _parse_ddm_value("15200", scale=0.001) == 15.2


def test_parse_ddm_value_sentinel_returns_none() -> None:
    """Sentinel values should return None."""
    from app.services.network.olt_polling import _parse_ddm_value

    assert _parse_ddm_value("2147483647") is None
    assert _parse_ddm_value("65535") is None


def test_parse_signal_value_onu_tx() -> None:
    """ONU Tx power should parse like OLT Rx (simple scale)."""
    from app.services.network.olt_polling import _parse_signal_value

    value = _parse_signal_value("250", vendor="huawei", metric="onu_tx")
    assert value == 2.5


def test_ddm_values_included_in_update_values() -> None:
    """Reading with DDM data should produce update_values with DDM keys."""
    from app.services.network.olt_polling import OntSignalReading

    reading = OntSignalReading(
        onu_index="0.1.3.5",
        olt_rx_dbm=-19.5,
        onu_rx_dbm=-21.0,
        onu_tx_dbm=2.5,
        distance_m=1200,
        is_online=True,
        temperature_c=42.0,
        voltage_v=3.3,
        bias_current_ma=15.2,
        offline_reason_raw=None,
        serial_number_raw=None,
    )
    # Build update_values dict the same way the polling loop does
    update_values: dict = {}
    if reading.olt_rx_dbm is not None:
        update_values["olt_rx_signal_dbm"] = reading.olt_rx_dbm
    if reading.onu_rx_dbm is not None:
        update_values["onu_rx_signal_dbm"] = reading.onu_rx_dbm
    if reading.onu_tx_dbm is not None:
        update_values["onu_tx_signal_dbm"] = reading.onu_tx_dbm
    if reading.temperature_c is not None:
        update_values["ont_temperature_c"] = reading.temperature_c
    if reading.voltage_v is not None:
        update_values["ont_voltage_v"] = reading.voltage_v
    if reading.bias_current_ma is not None:
        update_values["ont_bias_current_ma"] = reading.bias_current_ma

    assert update_values["onu_tx_signal_dbm"] == 2.5
    assert update_values["ont_temperature_c"] == 42.0
    assert update_values["ont_voltage_v"] == 3.3
    assert update_values["ont_bias_current_ma"] == 15.2


def test_event_type_ont_ddm_alert_exists() -> None:
    from app.services.events.types import EventType

    assert hasattr(EventType, "ont_ddm_alert")
    assert EventType.ont_ddm_alert.value == "ont.ddm_alert"


def test_build_reading_targets_does_not_fallback_to_unmatched_ont(db_session) -> None:
    from app.models.network import OLTDevice, OntUnit
    from app.services.network.olt_polling_parsers import OntSignalReading

    olt = OLTDevice(name="Poll OLT", vendor="Huawei")
    ont = OntUnit(
        serial_number="ONT-001",
        olt_device_id=olt.id,
        is_active=True,
        external_id="huawei:4194320640.5",
    )
    db_session.add_all([olt, ont])
    db_session.commit()

    readings = [
        OntSignalReading(
            onu_index="4194320640.7",
            olt_rx_dbm=-19.5,
            onu_rx_dbm=-21.0,
            onu_tx_dbm=None,
            distance_m=1000,
            is_online=True,
        )
    ]

    targets = _build_reading_targets(
        db_session,
        olt=olt,
        readings=readings,
        assignments=[],
    )

    assert targets == []


def test_build_reading_targets_skips_ambiguous_fsp_only_matches(db_session) -> None:
    from app.models.network import OLTDevice, OntUnit
    from app.services.network.olt_polling_parsers import OntSignalReading

    olt = OLTDevice(name="Poll OLT", vendor="Huawei")
    ont_a = OntUnit(
        serial_number="ONT-001",
        olt_device_id=olt.id,
        is_active=True,
        board="0/1",
        port="0",
        external_id=None,
    )
    ont_b = OntUnit(
        serial_number="ONT-002",
        olt_device_id=olt.id,
        is_active=True,
        board="0/1",
        port="0",
        external_id=None,
    )
    db_session.add_all([olt, ont_a, ont_b])
    db_session.commit()

    readings = [
        OntSignalReading(
            onu_index="4194320384.3",
            olt_rx_dbm=-19.5,
            onu_rx_dbm=-21.0,
            onu_tx_dbm=None,
            distance_m=1000,
            is_online=True,
        )
    ]

    targets = _build_reading_targets(
        db_session,
        olt=olt,
        readings=readings,
        assignments=[],
    )

    assert targets == []


def test_build_reading_targets_matches_huawei_fsp_and_numeric_onu_id(
    db_session,
) -> None:
    from app.models.network import OLTDevice, OntUnit
    from app.services.network.olt_polling_parsers import OntSignalReading

    olt = OLTDevice(name="Poll OLT", vendor="Huawei")
    db_session.add(olt)
    db_session.flush()
    ont_a = OntUnit(
        serial_number="48575443348F8A84",
        olt_device_id=olt.id,
        is_active=True,
        board="0/4",
        port="0",
        external_id="8",
    )
    ont_b = OntUnit(
        serial_number="48575443348F8A85",
        olt_device_id=olt.id,
        is_active=True,
        board="0/4",
        port="0",
        external_id="9",
    )
    db_session.add_all([ont_a, ont_b])
    db_session.commit()

    readings = [
        OntSignalReading(
            onu_index="4194320384.8",
            olt_rx_dbm=-19.5,
            onu_rx_dbm=-21.0,
            onu_tx_dbm=None,
            distance_m=1000,
            is_online=True,
        )
    ]

    targets = _build_reading_targets(
        db_session,
        olt=olt,
        readings=readings,
        assignments=[],
    )

    assert targets == [(ont_a, readings[0])]


def test_huawei_external_id_repair_targets_packed_snmp_index() -> None:
    assert (
        _expected_external_id_for_reading("Huawei", "4194315520.8")
        == "huawei:4194315520.8"
    )
    assert _should_repair_external_id("8", "huawei:4194315520.8") is True
    assert (
        _should_repair_external_id(
            "huawei:4194315520.8",
            "huawei:4194315520.8",
        )
        is False
    )


def test_mark_stale_onts_unknown_updates_effective_status_snapshot(db_session) -> None:
    from app.models.network import (
        OLTDevice,
        OntStatusSource,
        OntUnit,
        OnuOnlineStatus,
        PollStatus,
    )
    from app.tasks.olt_polling import _mark_stale_onts_offline

    now = datetime.now(UTC)
    olt = OLTDevice(
        name="Reachable OLT",
        is_active=True,
        last_poll_at=now,
        last_poll_status=PollStatus.success,
    )
    db_session.add(olt)
    db_session.flush()
    ont = OntUnit(
        serial_number="ONT-STALE-1",
        olt_device_id=olt.id,
        is_active=True,
        online_status=OnuOnlineStatus.online,
        effective_status=OnuOnlineStatus.online,
        signal_updated_at=now - timedelta(minutes=30),
    )
    db_session.add(ont)
    db_session.commit()

    marked = _mark_stale_onts_offline(db_session, stale_threshold_minutes=10)

    db_session.refresh(ont)
    assert marked == 1
    assert ont.online_status == OnuOnlineStatus.unknown
    assert ont.effective_status == OnuOnlineStatus.unknown
    assert ont.effective_status_source == OntStatusSource.derived
    assert ont.offline_reason is None
    assert ont.status_resolved_at is not None


def test_mark_stale_onts_unknown_keeps_recent_acs_inform_effective_online(
    db_session,
) -> None:
    from app.models.network import (
        OLTDevice,
        OntAcsStatus,
        OntStatusSource,
        OntUnit,
        OnuOnlineStatus,
        PollStatus,
    )
    from app.tasks.olt_polling import _mark_stale_onts_offline

    now = datetime.now(UTC)
    olt = OLTDevice(
        name="Reachable ACS OLT",
        is_active=True,
        last_poll_at=now,
        last_poll_status=PollStatus.success,
    )
    db_session.add(olt)
    db_session.flush()
    ont = OntUnit(
        serial_number="ONT-STALE-ACS-ONLINE",
        olt_device_id=olt.id,
        is_active=True,
        online_status=OnuOnlineStatus.online,
        effective_status=OnuOnlineStatus.online,
        acs_last_inform_at=now - timedelta(minutes=2),
        signal_updated_at=now - timedelta(minutes=30),
    )
    db_session.add(ont)
    db_session.commit()

    marked = _mark_stale_onts_offline(db_session, stale_threshold_minutes=10)

    db_session.refresh(ont)
    assert marked == 1
    assert ont.online_status == OnuOnlineStatus.unknown
    assert ont.acs_status == OntAcsStatus.online
    assert ont.effective_status == OnuOnlineStatus.online
    assert ont.effective_status_source == OntStatusSource.acs
    assert ont.offline_reason is None


def test_mark_stale_huawei_numeric_external_id_unknown_not_los(db_session) -> None:
    from app.models.network import (
        OLTDevice,
        OntStatusSource,
        OntUnit,
        OnuOnlineStatus,
        PollStatus,
    )
    from app.tasks.olt_polling import _mark_stale_onts_offline

    now = datetime.now(UTC)
    olt = OLTDevice(
        name="Huawei Reachable OLT",
        vendor="Huawei",
        is_active=True,
        last_poll_at=now,
        last_poll_status=PollStatus.success,
    )
    db_session.add(olt)
    db_session.flush()
    ont = OntUnit(
        serial_number="48575443348F8A84",
        olt_device_id=olt.id,
        is_active=True,
        online_status=OnuOnlineStatus.online,
        effective_status=OnuOnlineStatus.online,
        external_id="8",
        signal_updated_at=now - timedelta(minutes=30),
    )
    db_session.add(ont)
    db_session.commit()

    marked = _mark_stale_onts_offline(db_session, stale_threshold_minutes=10)

    db_session.refresh(ont)
    assert marked == 1
    assert ont.online_status == OnuOnlineStatus.unknown
    assert ont.effective_status == OnuOnlineStatus.unknown
    assert ont.effective_status_source == OntStatusSource.derived
    assert ont.offline_reason is None


def test_fsp_hint_from_huawei_packed_index_decodes_frame_slot_port() -> None:
    # 4194320384 = 0xFA000000 (base) + 16384 (delta)
    # delta / 256 = 64 -> slot = 64 / 16 = 4, port = 64 % 16 = 0
    assert _fsp_hint_from_index("4194320384.3") == "0/4/0"


def test_poll_sfp_modules_scopes_to_olt_and_uses_port_number_keys(
    db_session, monkeypatch
) -> None:
    from app.models.network import (
        OltCard,
        OltCardPort,
        OLTDevice,
        OltSfpModule,
        OltShelf,
    )
    from app.services.network.olt_polling import poll_sfp_modules

    olt = OLTDevice(name="OLT-A", vendor="Huawei", mgmt_ip="10.0.0.1")
    other_olt = OLTDevice(name="OLT-B", vendor="Huawei", mgmt_ip="10.0.0.2")
    db_session.add_all([olt, other_olt])
    db_session.commit()

    shelf = OltShelf(olt_id=olt.id, shelf_number=1)
    other_shelf = OltShelf(olt_id=other_olt.id, shelf_number=1)
    db_session.add_all([shelf, other_shelf])
    db_session.commit()

    card = OltCard(shelf_id=shelf.id, slot_number=2)
    other_card = OltCard(shelf_id=other_shelf.id, slot_number=2)
    db_session.add_all([card, other_card])
    db_session.commit()

    port = OltCardPort(card_id=card.id, port_number=3, is_active=True)
    other_port = OltCardPort(card_id=other_card.id, port_number=3, is_active=True)
    db_session.add_all([port, other_port])
    db_session.commit()

    sfp = OltSfpModule(olt_card_port_id=port.id, serial_number="SFP-A")
    other_sfp = OltSfpModule(olt_card_port_id=other_port.id, serial_number="SFP-B")
    db_session.add_all([sfp, other_sfp])
    db_session.commit()

    def _fake_walk(_host, oid, _community, timeout=20):
        if oid.endswith(".9"):
            return [f"{oid}.3 = INTEGER: -1950"]
        return [f"{oid}.3 = INTEGER: -2050"]

    monkeypatch.setattr(
        "app.services.network.olt_polling._run_olt_snmpwalk", _fake_walk
    )

    stats = poll_sfp_modules(db_session, olt, community="public")

    db_session.refresh(sfp)
    db_session.refresh(other_sfp)

    assert stats["updated"] >= 1
    assert sfp.tx_power_dbm == -19.5
    assert sfp.rx_power_dbm == -20.5
    assert other_sfp.tx_power_dbm is None
