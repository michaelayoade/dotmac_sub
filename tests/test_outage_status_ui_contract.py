from __future__ import annotations

from pathlib import Path

from app.services.topology.outage import OUTAGE_STATUS_VALUES, OutageStatus

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def test_outage_lifecycle_declares_one_complete_status_vocabulary() -> None:
    assert OUTAGE_STATUS_VALUES == tuple(status.value for status in OutageStatus)
    assert OUTAGE_STATUS_VALUES == (
        "open",
        "suspected",
        "confirmed",
        "clearing",
        "resolved",
        "discarded",
    )


def test_outage_templates_consume_shared_semantic_badge() -> None:
    paths = (
        "templates/admin/network/outages.html",
        "templates/admin/network/detected_outages.html",
        "templates/admin/network/detected_outages_notify.html",
    )
    for path in paths:
        template = _read(path)
        assert "status_presentation_badge" in template

    detected = _read("templates/admin/network/detected_outages.html")
    notify = _read("templates/admin/network/detected_outages_notify.html")
    assert "row.state == 'confirmed'" not in detected
    assert "row.state == 'clearing'" not in detected
    assert "{{ incident.status }}" not in notify


def test_outage_api_filters_and_projection_use_declared_owners() -> None:
    api = _read("app/api/crm.py")
    service = _read("app/services/crm_api.py")

    assert "OUTAGE_STATUS_VALUES" in api
    assert "_valid_status = (" not in api
    assert "OUTAGE_STATUS_VALUES" in service
    assert "_NARROWABLE_STATUSES" not in service
    assert '"status_presentation"' in service
