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
