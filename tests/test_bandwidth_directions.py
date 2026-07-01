"""Subscriber-perspective download/upload is derived in exactly one place.

bandwidth_samples store rx = NAS ingress = subscriber upload and tx = NAS egress
= subscriber download. to_subscriber_directions() is the single canonical mapping;
customer-facing code reads download_bps/upload_bps, never rx/tx.
"""

from app.services.bandwidth import (
    add_directions_to_series,
    to_subscriber_directions,
    with_subscriber_directions,
)


def test_to_subscriber_directions_maps_tx_to_download():
    download, upload = to_subscriber_directions(rx_bps=100, tx_bps=900)
    assert download == 900.0  # tx = NAS egress = download
    assert upload == 100.0  # rx = NAS ingress = upload


def test_with_subscriber_directions_adds_explicit_fields():
    out = with_subscriber_directions(
        {
            "current_rx_bps": 100,
            "current_tx_bps": 900,
            "peak_rx_bps": 200,
            "peak_tx_bps": 1800,
            "total_rx_bytes": 5,
            "total_tx_bytes": 50,
        }
    )
    assert out["download_bps"] == 900.0
    assert out["upload_bps"] == 100.0
    assert out["peak_download_bps"] == 1800.0
    assert out["peak_upload_bps"] == 200.0
    assert out["total_download_bytes"] == 50
    assert out["total_upload_bytes"] == 5


def test_add_directions_to_series_maps_each_point():
    result = add_directions_to_series(
        {"data": [{"rx_bps": 10, "tx_bps": 90}, {"rx_bps": 1, "tx_bps": 5}]}
    )
    assert result["data"][0]["download_bps"] == 90.0
    assert result["data"][0]["upload_bps"] == 10.0
    assert result["data"][1]["download_bps"] == 5.0


def test_bandwidth_stats_response_model_keeps_subscriber_directions():
    """The API response model must not drop the download/upload fields the
    service computes — mobile binds to them (rx/tx are NAS-perspective)."""
    from app.api.bandwidth import BandwidthStats

    stats = BandwidthStats(
        **with_subscriber_directions(
            {
                "current_rx_bps": 100.0,
                "current_tx_bps": 900.0,
                "peak_rx_bps": 200.0,
                "peak_tx_bps": 1800.0,
                "total_rx_bytes": 5,
                "total_tx_bytes": 50,
                "sample_count": 3,
            }
        )
    )
    assert stats.download_bps == 900.0
    assert stats.upload_bps == 100.0
    assert stats.peak_download_bps == 1800.0
    assert stats.peak_upload_bps == 200.0


def test_bandwidth_series_point_keeps_subscriber_directions():
    """The series response model must not strip the download/upload fields —
    the chart JS reads d.download_bps exclusively; without these the admin
    bandwidth chart renders a flat-zero series."""
    from datetime import UTC, datetime

    from app.api.bandwidth import BandwidthSeriesPoint

    result = add_directions_to_series(
        {"data": [{"timestamp": datetime.now(UTC), "rx_bps": 10, "tx_bps": 90}]}
    )
    point = BandwidthSeriesPoint(**result["data"][0])
    assert point.download_bps == 90.0
    assert point.upload_bps == 10.0


def test_live_event_payload_includes_subscriber_directions():
    """Both SSE producers (admin API + customer portal) must emit
    download_bps/upload_bps — the chart's live handler binds to them only."""
    from datetime import UTC, datetime

    from app.services.bandwidth import live_event_payload

    payload = live_event_payload({"rx_bps": 10, "tx_bps": 90}, datetime.now(UTC))
    assert payload["download_bps"] == 90.0
    assert payload["upload_bps"] == 10.0
    assert payload["rx_bps"] == 10.0
    assert payload["tx_bps"] == 90.0
    assert "timestamp" in payload
    # Defaults to a genuine sample (back-compat for producers that don't set it).
    assert payload["has_sample"] is True


def test_live_event_payload_has_sample_flag():
    """has_sample lets the chart show "Live" only on a real reading. A
    default-zero event from an unmapped sub (has_sample=False) must be
    distinguishable from a genuine idle 0 bps (has_sample=True)."""
    from datetime import UTC, datetime

    from app.services.bandwidth import live_event_payload

    now = datetime.now(UTC)
    idle = live_event_payload({"rx_bps": 0, "tx_bps": 0}, now, has_sample=True)
    assert idle["has_sample"] is True
    assert idle["download_bps"] == 0.0

    no_data = live_event_payload({"rx_bps": 0, "tx_bps": 0}, now, has_sample=False)
    assert no_data["has_sample"] is False
