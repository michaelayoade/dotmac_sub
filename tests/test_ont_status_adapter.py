from datetime import UTC, datetime
from types import SimpleNamespace

from app.models.network import OntAcsStatus, OntStatusSource, OnuOnlineStatus
from app.services.network.ont_status_adapter import (
    OntStatusResult,
    OpticalMetrics,
    SnmpStatusProvider,
    _apply_status_to_ont,
)


def test_snmp_status_provider_reads_ont_ddm_fields() -> None:
    fetched_at = datetime.now(UTC)
    ont = SimpleNamespace(
        olt_rx_signal_dbm=-21.5,
        onu_rx_signal_dbm=-22.1,
        onu_tx_signal_dbm=2.4,
        ont_temperature_c=42.0,
        ont_voltage_v=3.3,
        ont_bias_current_ma=11.2,
        distance_meters=440,
        signal_updated_at=fetched_at,
    )

    metrics = SnmpStatusProvider().get_optical_metrics(None, ont)  # type: ignore[arg-type]

    assert metrics.olt_rx_dbm == -21.5
    assert metrics.onu_rx_dbm == -22.1
    assert metrics.onu_tx_dbm == 2.4
    assert metrics.temperature_c == 42.0
    assert metrics.voltage_v == 3.3
    assert metrics.bias_current_ma == 11.2
    assert metrics.distance_m == 440
    assert metrics.fetched_at == fetched_at


def test_apply_status_to_ont_writes_ont_ddm_fields() -> None:
    ont = SimpleNamespace()
    result = OntStatusResult(
        online_status=OnuOnlineStatus.online,
        acs_status=OntAcsStatus.unknown,
        status_source=OntStatusSource.olt,
        optical_metrics=OpticalMetrics(
            onu_tx_dbm=2.7,
            temperature_c=39.5,
            voltage_v=3.2,
            bias_current_ma=10.4,
        ),
    )

    _apply_status_to_ont(ont, result)  # type: ignore[arg-type]

    assert ont.onu_tx_signal_dbm == 2.7
    assert ont.ont_temperature_c == 39.5
    assert ont.ont_voltage_v == 3.2
    assert ont.ont_bias_current_ma == 10.4


def test_snmp_status_provider_uses_shared_acs_override_rule() -> None:
    now = datetime.now(UTC)
    ont = SimpleNamespace(
        online_status=OnuOnlineStatus.offline,
        acs_last_inform_at=now,
        tr069_acs_server_id="acs-1",
        tr069_acs_server=None,
        olt_device=None,
    )

    result = SnmpStatusProvider().get_status(None, ont)  # type: ignore[arg-type]

    assert result.online_status == OnuOnlineStatus.online
    assert result.acs_status == OntAcsStatus.online
    assert result.status_source == OntStatusSource.acs
