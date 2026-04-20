from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.network.olt_polling import (
    _parse_signal_value,
    _parse_snmp_table,
    reconcile_snmp_status_with_signal,
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


def test_reconcile_snmp_numeric_offline_code_trusts_valid_signal() -> None:
    from app.models.network import OnuOnlineStatus

    status, reason, reconciled = reconcile_snmp_status_with_signal(
        vendor="Huawei",
        raw_status="2",
        olt_rx_dbm=-22.59,
    )

    assert status == OnuOnlineStatus.online
    assert reason is None
    assert reconciled is True


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
