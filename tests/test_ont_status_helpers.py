from datetime import UTC, datetime
from types import SimpleNamespace

from app.models.network import OntStatusSource, OnuOnlineStatus
from app.services.network.ont_status import (
    get_ont_status,
    get_optical_metrics,
)


def test_get_optical_metrics_reads_persisted_ont_ddm_fields() -> None:
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

    metrics = get_optical_metrics(None, ont)  # type: ignore[arg-type]

    assert metrics.olt_rx_dbm == -21.5
    assert metrics.onu_rx_dbm == -22.1
    assert metrics.onu_tx_dbm == 2.4
    assert metrics.temperature_c == 42.0
    assert metrics.voltage_v == 3.3
    assert metrics.bias_current_ma == 11.2
    assert metrics.distance_m == 440
    assert metrics.fetched_at == fetched_at


def test_get_ont_status_uses_shared_acs_override_rule() -> None:
    now = datetime.now(UTC)
    ont = SimpleNamespace(
        olt_status=OnuOnlineStatus.offline,
        olt_status_seen_at=now,
        acs_last_inform_at=now,
        tr069_acs_server_id="acs-1",
        tr069_acs_server=None,
        olt_device=None,
        consecutive_offline_polls=3,
    )

    result = get_ont_status(None, ont)  # type: ignore[arg-type]

    assert result.effective_status == OnuOnlineStatus.online
    assert result.status_source == OntStatusSource.acs
