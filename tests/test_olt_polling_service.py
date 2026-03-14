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


def test_parse_signal_value_huawei_onu_rx_offset_decoding() -> None:
    value = _parse_signal_value("7113", vendor="huawei", metric="onu_rx")
    assert value == -28.87


def test_parse_signal_value_sentinel_is_none() -> None:
    value = _parse_signal_value("2147483647", vendor="huawei", metric="olt_rx")
    assert value is None
