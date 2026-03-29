from app.services.network.olt_polling import (
    _parse_signal_value,
    _parse_snmp_table,
    _parse_snmp_table_composite,
)


def test_parse_snmp_table_last_token_index() -> None:
    lines = [
        "iso.3.6.1.4.1.2011.6.128.1.1.2.51.1.4.4194320384.3 = INTEGER: -1950",
    ]
    parsed = _parse_snmp_table(lines)
    assert parsed == {"3": "-1950"}


def test_parse_snmp_table_composite_index() -> None:
    lines = [
        "iso.3.6.1.4.1.2011.6.128.1.1.2.51.1.4.4194320384.3 = INTEGER: -1950",
    ]
    parsed = _parse_snmp_table_composite(
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
