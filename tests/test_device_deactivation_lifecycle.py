"""Deactivating a device is one atomic lifecycle slice, not a flag write.

The defect these tests pin down: ``is_active = False`` used to be written by
several callers directly, and each got only half a deactivation.

  1. ``collect_devices`` filtered the flag, and the projection reconciler
     deletes any row the derivation stops returning — so deactivation ERASED
     the device from the staff ledger.
  2. The topology warmer only visits pollable devices and nothing ever decayed
     ``live_status``, so a device warmed to ``up`` that left the pollable set
     stayed ``up`` forever.
  3. ``classify_node`` reads mgmt ``up`` + zero online customers as
     ``service_fault`` ("NOT an area outage"), so that frozen ``up`` silently
     vetoed outage detection and the customer surface reported the cabinet
     healthy indefinitely.

Release gate under test: an inactive or stale device can never project ``up``.

IMPORTANT: none of the NetworkDevice fixtures here set ``source``. The old
drift filter keyed on the retired ``zabbix_reconcile`` provenance and looked
healthy under test only because every existing fixture stamped it. Fixtures
without a source are what prove the replacement is authority-neutral.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.network_monitoring import (
    DeviceProjection,
    DeviceStatus,
    NetworkDevice,
)
from app.services import device_projection_reconcile as reconcile_mod
from app.services.device_operational_status import (
    NOT_WORKING,
    WORKING,
    derive_operational_status,
)
from app.services.device_projection_reconcile import (
    ReconcileDeviceProjectionsCommand,
    _gated_status,
    reconcile_device_projections,
)
from app.services.network_monitoring import (
    NetworkDevices,
    set_network_device_active,
)
from app.services.owner_commands import CommandContext
from app.services.topology.health_classifier import (
    NODE_OUTAGE,
    SERVICE_FAULT,
    classify_node,
)
from app.services.topology.live_status import (
    STALE_POLL_AFTER_SECONDS,
    trusted_live_status,
    warm_topology_status,
)
from app.services.web_network_core_devices_inventory import collect_devices

_ip_counter = iter(range(1, 10_000))


def _device(db, name, **kw):
    """A monitoring device with NO ``source`` — deliberately (see module docstring)."""
    kw.setdefault("is_active", True)
    kw.setdefault("ping_enabled", True)
    if "mgmt_ip" not in kw:
        n = next(_ip_counter)
        kw["mgmt_ip"] = f"10.88.{n // 250}.{n % 250 + 1}"
    device = NetworkDevice(name=name, **kw)
    db.add(device)
    db.flush()
    return device


def _fresh_up(db, name, **kw):
    """A device the poller currently observes as reachable."""
    now = datetime.now(UTC)
    return _device(
        db,
        name,
        live_status="up",
        live_status_at=now,
        last_ping_ok=True,
        last_ping_at=now,
        **kw,
    )


# ── 1. The owned transition ─────────────────────────────────────────────────


def test_deactivation_decays_live_status_instead_of_freezing_it(db_session):
    device = _fresh_up(db_session, "gwarimpa-4")

    set_network_device_active(db_session, device, False, reason="test")

    assert device.is_active is False
    # The whole point: the assertion is WITHDRAWN, not frozen at "up".
    assert device.live_status == "unknown"
    assert device.live_status_at is not None


def test_reactivation_does_not_resurrect_the_pre_deactivation_verdict(db_session):
    device = _fresh_up(db_session, "gwarimpa-5")
    set_network_device_active(db_session, device, False, reason="test")

    set_network_device_active(db_session, device, True, reason="test")

    assert device.is_active is True
    # Re-admission does not re-assert reachability; the next poll does.
    assert device.live_status == "unknown"


def test_transition_is_idempotent_and_repairs_a_previously_frozen_row(db_session):
    # A row deactivated before this transition existed: inactive but still
    # claiming "up". Re-applying the current state must repair it.
    device = _fresh_up(db_session, "gwarimpa-6", is_active=False)
    assert device.live_status == "up"

    set_network_device_active(db_session, device, False, reason="test")

    assert device.live_status == "unknown"


def test_soft_delete_routes_through_the_transition(db_session):
    device = _fresh_up(db_session, "manual-delete")

    NetworkDevices.delete(db_session, str(device.id))

    assert device.is_active is False
    assert device.live_status == "unknown"
    # Soft delete: the row is still there to be listed.
    assert db_session.get(NetworkDevice, device.id) is not None


def test_inventory_update_payload_routes_admission_through_the_transition(db_session):
    from app.schemas.network_monitoring import NetworkDeviceUpdate

    device = _fresh_up(db_session, "payload-deactivate")

    NetworkDevices.update(
        db_session, str(device.id), NetworkDeviceUpdate(is_active=False)
    )

    assert device.is_active is False
    assert device.live_status == "unknown"


def test_router_delete_cascade_decays_the_linked_monitoring_device(db_session):
    from app.models.router_management import Router
    from app.services.router_management.inventory import RouterInventory

    device = _fresh_up(db_session, "edge-router-node")
    router = Router(
        name="edge-router",
        management_ip=device.mgmt_ip,
        network_device_id=device.id,
        is_active=True,
    )
    db_session.add(router)
    db_session.flush()

    RouterInventory.delete(db_session, router.id)

    db_session.refresh(device)
    assert device.is_active is False
    assert device.live_status == "unknown"


# ── 2. Inactive devices stay visible in inventory ───────────────────────────


def test_collect_devices_keeps_a_deactivated_core_device_with_an_inactive_marker(
    db_session,
):
    live = _fresh_up(db_session, "aa-live-core")
    dead = _fresh_up(db_session, "aa-dead-core")
    set_network_device_active(db_session, dead, False, reason="test")

    rows = {row["id"]: row for row in collect_devices(db_session)}

    # The regression: the deactivated device must NOT disappear.
    assert str(dead.id) in rows
    assert rows[str(dead.id)]["lifecycle_state"] == "inactive"
    assert rows[str(dead.id)]["status"] == NOT_WORKING
    assert rows[str(live.id)]["lifecycle_state"] == "active"


# ── 3. Release gate: inactive/stale can never project "up" ──────────────────


def _command() -> ReconcileDeviceProjectionsCommand:
    return ReconcileDeviceProjectionsCommand(
        context=CommandContext.system(
            actor="pytest:device_lifecycle",
            scope="network:test",
            reason="verify inactive devices stay projected",
        )
    )


def _projection_rows(db):
    return {
        (r.device_type, r.source_id): r
        for r in db.execute(select(DeviceProjection)).scalars()
    }


def test_reconcile_keeps_an_inactive_device_and_marks_it_instead_of_pruning(
    db_session,
):
    device = _fresh_up(db_session, "bb-dead-core")
    set_network_device_active(db_session, device, False, reason="test")

    reconcile_device_projections(db_session, _command())

    rows = _projection_rows(db_session)
    row = rows[("core", str(device.id))]
    assert row.lifecycle_state == "inactive"
    assert row.operational_status == "not_working"


def test_gated_status_forces_inactive_devices_to_not_working():
    assert _gated_status("inactive", "working") == "not_working"
    assert _gated_status("inactive", "not_working") == "not_working"
    assert _gated_status("active", "working") == "working"


def test_reconcile_gates_an_upstream_derivation_that_claims_inactive_is_working(
    db_session, monkeypatch
):
    # Belt-and-braces: even if the derivation regressed and reported an
    # inactive device as working, the projection must not carry it.
    bad = {
        "id": str(uuid.uuid4()),
        "name": "liar",
        "type": "core",
        "serial_number": None,
        "ip_address": None,
        "vendor": None,
        "model": None,
        "status": "working",
        "operational_reason": None,
        "last_seen": None,
        "subscriber": None,
        "class_facts": None,
        "lifecycle_state": "inactive",
    }
    monkeypatch.setattr(reconcile_mod, "collect_devices", lambda db: [bad])

    reconcile_device_projections(db_session, _command())

    row = _projection_rows(db_session)[("core", bad["id"])]
    assert row.operational_status == "not_working"


def test_database_refuses_an_inactive_working_projection_row(db_session):
    """The release gate as a schema invariant, not just a code path."""
    db_session.add(
        DeviceProjection(
            device_type="core",
            source_id=str(uuid.uuid4()),
            operational_status="working",
            lifecycle_state="inactive",
            refreshed_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


# ── 4. The freshness gate on the read path ──────────────────────────────────


def test_trusted_live_status_passes_a_fresh_pollable_up(db_session):
    device = _fresh_up(db_session, "cc-fresh")
    assert trusted_live_status(device) == "up"


def test_trusted_live_status_decays_up_on_an_inactive_device(db_session):
    device = _fresh_up(db_session, "cc-inactive", is_active=False)
    assert trusted_live_status(device) == "unknown"


def test_trusted_live_status_decays_up_when_the_device_left_the_pollable_set(
    db_session,
):
    # Checks disabled: the warmer never visits it again, so its "up" is frozen.
    device = _fresh_up(db_session, "cc-unpollable", ping_enabled=False)
    device.snmp_enabled = False
    assert trusted_live_status(device) == "unknown"


def test_trusted_live_status_decays_up_when_the_observation_went_stale(db_session):
    stale = datetime.now(UTC) - timedelta(seconds=STALE_POLL_AFTER_SECONDS * 3)
    device = _device(
        db_session,
        "cc-stale",
        live_status="up",
        live_status_at=stale,
        last_ping_ok=True,
        last_ping_at=stale,
    )
    assert trusted_live_status(device) == "unknown"


def test_trusted_live_status_decays_up_when_the_warmer_is_dead(db_session):
    # No per-node observation timestamp at all -> fall back to the warmer's
    # dead-man switch rather than trusting an unsupported positive.
    device = _device(db_session, "cc-no-obs", live_status="up")
    assert trusted_live_status(device, warm_stale=True) == "unknown"
    assert trusted_live_status(device, warm_stale=False) == "up"


def test_trusted_live_status_keeps_a_stale_down(db_session):
    """The gate is asymmetric on purpose.

    Stale ``up`` fails dangerously (it vetoes outage detection and tells the
    customer everything is fine). Stale ``down`` fails safely — it opens an
    incident an operator can see and close. Decaying both would let a dead
    warmer SUPPRESS real outages.
    """
    stale = datetime.now(UTC) - timedelta(seconds=STALE_POLL_AFTER_SECONDS * 3)
    device = _device(
        db_session,
        "cc-stale-down",
        live_status="down",
        live_status_at=stale,
        last_ping_ok=False,
        last_ping_at=stale,
    )
    assert trusted_live_status(device) == "down"


# ── 5. The frozen "up" no longer vetoes outage detection ────────────────────


def test_frozen_up_on_a_deactivated_node_classifies_as_node_outage(db_session):
    """The customer-visible consequence, end to end.

    Before: an inactive node still holding ``live_status='up'`` classified
    ``service_fault`` — "reachable but serving nobody, NOT an area outage" —
    so no incident opened and every customer behind it was told to reboot
    their router. It must now classify ``node_outage``.
    """
    node = _fresh_up(db_session, "dd-frozen-up", is_active=False)
    assert node.live_status == "up"  # frozen, exactly as production had it

    assert classify_node(node, online_count=0, had_prior_life=True) == NODE_OUTAGE


def test_an_actually_reachable_node_still_classifies_as_service_fault(db_session):
    # The guard must not swallow the genuine up/up/down row.
    node = _fresh_up(db_session, "dd-real-up")
    assert (
        classify_node(node, online_count=0, had_prior_life=True, warm_stale=False)
        == SERVICE_FAULT
    )


def test_a_dead_warmer_cannot_certify_a_node_reachable(db_session):
    node = _device(db_session, "dd-warmer-dead", live_status="up")
    assert (
        classify_node(node, online_count=0, had_prior_life=True, warm_stale=True)
        == NODE_OUTAGE
    )


# ── 6. The warmer decays what it no longer visits ───────────────────────────


def test_warm_topology_status_decays_nodes_that_left_the_pollable_set(db_session):
    """Repairs rows already frozen in production, not just new deactivations."""
    warmed = _fresh_up(db_session, "ee-still-polled")
    frozen = _fresh_up(db_session, "ee-frozen")
    # Simulate the pre-fix state: deactivated with a raw flag write, so the
    # derived cache was never decayed.
    frozen.is_active = False
    db_session.flush()
    assert frozen.live_status == "up"

    result = warm_topology_status(db_session)

    assert result["decayed"] >= 1
    assert frozen.live_status == "unknown"
    assert warmed.live_status == "up"


# ── 7. network.device_state honours admission and the poll clock ────────────


def test_derive_operational_status_marks_an_inactive_device_not_working(db_session):
    device = _fresh_up(db_session, "ff-inactive", is_active=False)

    operational = derive_operational_status(device, warm_stale=False)

    assert operational.status == NOT_WORKING
    assert operational.reason == "admin_inactive"
    # Deliberate removal from service is not an alarm.
    assert operational.alarming is False


def test_a_stably_up_device_is_working_despite_an_old_dwell_clock(db_session):
    """``live_status_at`` is a dwell clock, not a freshness signal.

    The warmer stamps it only when the state CHANGES, so a device up for a week
    carries a week-old ``live_status_at``. Reading that as observation age
    marked every stably-healthy device ``verification_expired``. Freshness must
    come from the poll clock.
    """
    now = datetime.now(UTC)
    device = _device(
        db_session,
        "ff-stable-up",
        status=DeviceStatus.online,
        live_status="up",
        live_status_at=now - timedelta(days=7),  # entered "up" a week ago
        last_ping_ok=True,
        last_ping_at=now,  # ...and was polled a moment ago
    )

    operational = derive_operational_status(device, warm_stale=False, now=now)

    assert operational.status == WORKING
    assert operational.reason == "observed_working"


def test_a_device_whose_poll_clock_stopped_is_not_working(db_session):
    now = datetime.now(UTC)
    device = _device(
        db_session,
        "ff-poll-stopped",
        live_status="up",
        live_status_at=now,  # dwell clock looks fresh...
        last_ping_ok=True,
        last_ping_at=now - timedelta(days=2),  # ...but nothing has polled it
    )

    operational = derive_operational_status(device, warm_stale=False, now=now)

    assert operational.status == NOT_WORKING
    assert operational.reason == "verification_expired"


# ── 8. The drift report is authority-neutral ────────────────────────────────


def test_unmatched_node_report_sees_devices_with_no_source_stamp(db_session):
    """The trap this whole area fell into.

    The old query required ``source == 'zabbix_reconcile'`` — provenance from
    an importer deleted on 2026-07-10 — so it was blind to every device created
    since. It only looked healthy under test because every existing fixture
    stamped that source. This device deliberately does not.
    """
    from app.services.topology.gaps import topology_gaps

    ghost = _device(db_session, "zz-no-source-ghost", matched_device_id=None)
    assert ghost.source is None

    gaps = topology_gaps(db_session)

    assert ghost.id in {node.id for node in gaps.unmatched_nodes}


def test_projection_drift_reports_a_device_missing_from_the_projection(db_session):
    from app.services.topology.gaps import (
        DRIFT_MISSING_PROJECTION,
        projection_drift_rows,
    )

    device = _device(db_session, "zz-unprojected")

    drift = {row["id"]: row["drift"] for row in projection_drift_rows(db_session)}

    assert drift.get(str(device.id)) == DRIFT_MISSING_PROJECTION


def test_projection_drift_reports_an_orphan_projection_row(db_session):
    from app.services.topology.gaps import (
        DRIFT_ORPHAN_PROJECTION,
        projection_drift_rows,
    )

    orphan_id = str(uuid.uuid4())
    db_session.add(
        DeviceProjection(
            device_type="core",
            source_id=orphan_id,
            name="zz-orphan",
            operational_status="not_working",
            lifecycle_state="active",
            refreshed_at=datetime.now(UTC),
        )
    )
    db_session.flush()

    drift = {row["id"]: row["drift"] for row in projection_drift_rows(db_session)}

    assert drift.get(orphan_id) == DRIFT_ORPHAN_PROJECTION


def test_projection_drift_reports_a_reconciler_that_stopped_converging(db_session):
    from app.services.topology.gaps import (
        DRIFT_STALE_PROJECTION,
        PROJECTION_STALE_AFTER,
        projection_drift_rows,
    )

    device = _device(db_session, "zz-stale-projection")
    db_session.add(
        DeviceProjection(
            device_type="core",
            source_id=str(device.id),
            name=device.name,
            ip_address=device.mgmt_ip,
            operational_status="not_working",
            lifecycle_state="active",
            refreshed_at=datetime.now(UTC) - PROJECTION_STALE_AFTER * 4,
        )
    )
    db_session.flush()

    drift = {row["id"]: row["drift"] for row in projection_drift_rows(db_session)}

    assert drift.get(str(device.id)) == DRIFT_STALE_PROJECTION
