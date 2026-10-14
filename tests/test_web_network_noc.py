"""The NOC queue merges the open outage / mismatch / alarm owners into one list."""

from fastapi.templating import Jinja2Templates

from app.services.web_network_noc import noc_queue_data
from app.web.brand_globals import install_brand_jinja_global


def test_noc_queue_shape_and_empty(db_session):
    data = noc_queue_data(db_session)
    assert set(data["counts"]) == {
        "total",
        "outages",
        "mismatches",
        "alarms",
        "collectors",
    }
    assert data["counts"]["total"] == len(data["items"])
    # empty test DB → nothing in queue, but all three owner reads must run cleanly
    assert data["items"] == []
    assert data["counts"]["total"] == 0


def test_noc_operational_evidence_template_compiles() -> None:
    install_brand_jinja_global()
    env = Jinja2Templates(directory="templates").env

    env.get_template("admin/network/noc/index.html")


def test_typed_poller_failure_retains_noc_serialization(
    db_session, monkeypatch
) -> None:
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from app.services import operational_checks
    from app.services.poller_health import (
        PollerDeviceFailure,
        PollerFailureCategory,
        PollerSnapshot,
    )

    now = datetime.now(UTC)
    device_id = uuid4()
    failure = PollerDeviceFailure(
        device_id,
        "NAS",
        "192.0.2.1",
        PollerFailureCategory.TIMEOUT,
        2,
        now,
        None,
        now + timedelta(seconds=30),
        4,
    )
    snapshot = PollerSnapshot(now, 0.125, 12, 1, 0, 1, (failure,))
    monkeypatch.setattr(
        operational_checks, "bandwidth_poller_snapshot", lambda: snapshot
    )

    data = noc_queue_data(db_session)

    (row,) = (row for row in data["items"] if row["kind"] == "collector")
    assert row["id"] == str(device_id)
    assert row["title"] == "NAS"
    assert row["result"] == "Connection timed out"
    assert row["count"] == 4
    assert row["next_attempt_at"] == failure.next_attempt_at
    assert row["when"] == now
    assert row["url"] == f"/admin/network/nas/{device_id}"
    assert data["counts"]["collectors"] == 1
