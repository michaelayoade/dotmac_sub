"""Detected-outage incident reconcile — design §7.6.

The debounced, classifier-driven lifecycle: suspected -> confirmed -> clearing
-> resolved (+ discarded), with impact-scaled confirm windows, a fixed resolve
window, localization-drift re-pointing, and idempotent re-runs. Firing stays
gated; this layer only persists the lifecycle and emits events. Time is frozen
and passed explicitly (``now=``) — the topology services never call
``datetime.now`` in the debounce math.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network_monitoring import (
    DeviceRole,
    NetworkDevice,
    NetworkTopologyLink,
    OutageIncident,
)
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Subscriber
from app.services.topology import outage as outage_svc
from app.services.topology.outage import (
    CLASSIFIER_SOURCE,
    declare_outage,
    list_open_incidents,
)
from app.services.topology.outage_reconcile import (
    confirm_window_seconds,
    reconcile_detected_outages,
)

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)

# zabbix_hostid carries a partial-unique index (uq_network_devices_zabbix_hostid),
# so every zabbix-linked test node needs a distinct host id.
_HOSTID = iter(range(1, 100_000))


def _next_hostid() -> str:
    return str(next(_HOSTID))


def _naive(dt):
    """Compare lifecycle stamps tz-agnostically — DateTime(timezone=True) round-
    trips tz-naive on SQLite and tz-aware on Postgres."""
    return dt.replace(tzinfo=None) if dt is not None else None


# --- helpers (mirror test_health_classifier) ------------------------------


def _nas(db, name, ip):
    nas = NasDevice(name=name, management_ip=ip)
    db.add(nas)
    db.flush()
    return nas


def _node(
    db,
    name,
    *,
    mtype="nas",
    mid=None,
    role=DeviceRole.edge,
    live_status="down",
    zabbix=None,
    pop=None,
):
    n = NetworkDevice(
        name=name,
        matched_device_type=mtype,
        matched_device_id=mid,
        role=role,
        is_active=True,
        live_status=live_status,
        zabbix_hostid=zabbix or _next_hostid(),
        pop_site_id=pop.id if pop is not None else None,
    )
    db.add(n)
    db.flush()
    return n


def _link(db, a, b):
    db.add(
        NetworkTopologyLink(
            source_device_id=a.id,
            target_device_id=b.id,
            source="lldp_neighbor",
            is_active=True,
        )
    )
    db.flush()


def _sub(db, offer_id, nas_id):
    s = Subscriber(first_name="A", last_name="B", email=f"{uuid.uuid4().hex}@ex.com")
    db.add(s)
    db.flush()
    sub = Subscription(
        subscriber_id=s.id,
        offer_id=offer_id,
        status=SubscriptionStatus.active,
        provisioning_nas_device_id=nas_id,
    )
    db.add(sub)
    db.flush()
    return sub


def _session(db, subscription, nas_device_id, *, now=NOW, age=timedelta(0)):
    ts = now - age
    ras = RadiusActiveSession(
        subscriber_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        nas_device_id=nas_device_id,
        username="u",
        acct_session_id=uuid.uuid4().hex,
        session_start=ts,
        last_update=ts,
    )
    db.add(ras)
    db.flush()
    return ras


def _dark_nas_node(db, offer_id, name, n_subs):
    """A zabbix-linked NAS node, mgmt down, ``n_subs`` provisioned + all offline
    (no fresh session) -> classifies node_outage."""
    nas = _nas(db, f"NAS-{name}", f"10.0.{abs(hash(name)) % 250}.1")
    node = _node(db, name, mid=nas.id, live_status="down")
    for _ in range(n_subs):
        _sub(db, offer_id, nas.id)
    return node, nas


def _classifier_incidents(db):
    return (
        db.query(OutageIncident)
        .filter(OutageIncident.detection_source == CLASSIFIER_SOURCE)
        .all()
    )


def _capture_events(monkeypatch):
    """Record every lifecycle event kind emitted through the outage service."""
    kinds: list[str] = []
    orig = outage_svc._emit_outage_event

    def _spy(session, incident, kind):  # noqa: ANN001
        kinds.append(kind)
        return orig(session, incident, kind)

    monkeypatch.setattr(outage_svc, "_emit_outage_event", _spy)
    return kinds


# --- window function (pure) -----------------------------------------------


def test_confirm_window_scales_with_impact():
    kw = dict(small=600, med=360, large=0, threshold_med=5, threshold_large=20)
    assert confirm_window_seconds(25, **kw) == 0  # large -> now
    assert confirm_window_seconds(20, **kw) == 0
    assert confirm_window_seconds(19, **kw) == 360  # medium
    assert confirm_window_seconds(5, **kw) == 360
    assert confirm_window_seconds(4, **kw) == 600  # small
    assert confirm_window_seconds(0, **kw) == 600


# --- suspected opens on node_outage ----------------------------------------


def test_suspected_opens_on_node_outage(db_session, catalog_offer):
    _dark_nas_node(db_session, catalog_offer.id, "edge-a", 3)
    counters = reconcile_detected_outages(db_session, now=NOW)

    incidents = _classifier_incidents(db_session)
    assert len(incidents) == 1
    inc = incidents[0]
    assert inc.status == "suspected"  # 3 < 5 -> small window, waits
    assert inc.detection_source == "classifier"
    assert inc.classification == "node_outage"
    assert inc.affected_count == 3
    assert _naive(inc.suspected_at) == _naive(NOW)
    assert inc.confirmed_at is None
    assert counters["suspected_opened"] == 1
    assert counters["confirmed"] == 0


def test_lifecycle_event_payload_carries_backward_compatible_provenance(
    db_session, monkeypatch
):
    import app.services.events as events_pkg

    payloads = []
    monkeypatch.setattr(
        events_pkg,
        "emit_event",
        lambda session, event_type, payload, **kw: payloads.append(payload),
    )
    node = _node(db_session, "event-node", live_status="down")

    outage_svc.open_classifier_incident(db_session, root_node=node, now=NOW)

    payload = payloads[-1]
    assert payload["detection_source"] == "manual"  # legacy auto/manual field
    assert payload["provenance"] == "classifier"


# --- false-positive suppression: suspected -> discarded --------------------


def test_suspected_discarded_when_recovery_beats_confirm(
    db_session, catalog_offer, monkeypatch
):
    kinds = _capture_events(monkeypatch)
    node, nas = _dark_nas_node(db_session, catalog_offer.id, "edge-b", 3)
    reconcile_detected_outages(db_session, now=NOW)  # -> suspected (small window)
    inc = _classifier_incidents(db_session)[0]
    assert inc.status == "suspected"

    # Recover well before W_confirm (600s): node back up.
    node.live_status = "up"
    db_session.flush()
    counters = reconcile_detected_outages(db_session, now=NOW + timedelta(seconds=60))

    inc = _classifier_incidents(db_session)[0]
    assert inc.status == "discarded"
    assert counters["discarded"] == 1
    assert counters["confirmed"] == 0
    # No confirmed incident, and crucially no confirmed event ever fired.
    assert "outage.confirmed" not in kinds
    assert "outage.suspected" in kinds and "outage.discarded" in kinds


# --- scaled confirm: large confirms now, small waits -----------------------


def test_large_impact_confirms_immediately(db_session, catalog_offer):
    _dark_nas_node(db_session, catalog_offer.id, "edge-big", 25)  # >= 20 -> now
    counters = reconcile_detected_outages(db_session, now=NOW)

    inc = _classifier_incidents(db_session)[0]
    assert inc.status == "confirmed"
    assert _naive(inc.confirmed_at) == _naive(NOW)
    assert counters["confirmed"] == 1


def test_small_impact_confirms_only_after_window(db_session, catalog_offer):
    _dark_nas_node(db_session, catalog_offer.id, "edge-sm", 3)  # < 5 -> 600s
    reconcile_detected_outages(db_session, now=NOW)
    assert _classifier_incidents(db_session)[0].status == "suspected"

    # Still short of the window -> still suspected.
    reconcile_detected_outages(db_session, now=NOW + timedelta(seconds=300))
    assert _classifier_incidents(db_session)[0].status == "suspected"

    # Past W_confirm -> confirmed.
    counters = reconcile_detected_outages(db_session, now=NOW + timedelta(seconds=601))
    inc = _classifier_incidents(db_session)[0]
    assert inc.status == "confirmed"
    assert counters["confirmed"] == 1


# --- confirmed -> clearing -> resolved, and reopen -------------------------


def test_confirmed_clearing_resolved_with_mttr(db_session, catalog_offer):
    node, nas = _dark_nas_node(db_session, catalog_offer.id, "edge-res", 25)
    reconcile_detected_outages(db_session, now=NOW)  # confirmed immediately
    inc = _classifier_incidents(db_session)[0]
    assert inc.status == "confirmed"
    confirmed_at = inc.confirmed_at

    # Recover -> clearing.
    node.live_status = "up"
    db_session.flush()
    t_clear = NOW + timedelta(minutes=5)
    reconcile_detected_outages(db_session, now=t_clear)
    inc = _classifier_incidents(db_session)[0]
    assert inc.status == "clearing"
    assert _naive(inc.cleared_at) == _naive(t_clear)

    # Sustained recovery past W_resolve (300s) -> resolved.
    t_resolve = t_clear + timedelta(seconds=301)
    reconcile_detected_outages(db_session, now=t_resolve)
    inc = _classifier_incidents(db_session)[0]
    assert inc.status == "resolved"
    assert _naive(inc.resolved_at) == _naive(t_resolve)
    # MTTR = resolved_at - confirmed_at, derivable and positive.
    mttr = inc.resolved_at - inc.confirmed_at
    assert mttr.total_seconds() > 0
    assert _naive(inc.confirmed_at) == _naive(confirmed_at)
    # Terminal -> no longer surfaced as open.
    assert inc.id not in {i.id for i in list_open_incidents(db_session)}


def test_clearing_reopens_on_redarken_within_resolve_window(db_session, catalog_offer):
    node, nas = _dark_nas_node(db_session, catalog_offer.id, "edge-hyst", 25)
    reconcile_detected_outages(db_session, now=NOW)  # confirmed
    node.live_status = "up"
    db_session.flush()
    reconcile_detected_outages(db_session, now=NOW + timedelta(minutes=5))  # clearing
    assert _classifier_incidents(db_session)[0].status == "clearing"

    # Re-darken inside the resolve window -> reopen (hysteresis).
    node.live_status = "down"
    db_session.flush()
    counters = reconcile_detected_outages(
        db_session, now=NOW + timedelta(minutes=5, seconds=60)
    )
    inc = _classifier_incidents(db_session)[0]
    assert inc.status == "confirmed"
    assert inc.cleared_at is None
    assert counters["reopened"] == 1


# --- localization drift re-points root on the SAME incident ----------------


def test_localization_drift_repoints_root_no_duplicate(db_session, catalog_offer):
    # core(up) -> A(down) -> B(down); B is deeper. Both have offline customers.
    core = _node(db_session, "core", mtype=None, role=DeviceRole.core, live_status="up")
    nas_a = _nas(db_session, "NAS-A", "10.1.0.1")
    nas_b = _nas(db_session, "NAS-B", "10.1.0.2")
    a = _node(db_session, "A", mid=nas_a.id, live_status="down")
    b = _node(db_session, "B", mid=nas_b.id, live_status="down")
    _link(db_session, core, a)
    _link(db_session, a, b)
    for _ in range(6):
        _sub(db_session, catalog_offer.id, nas_a.id)
        _sub(db_session, catalog_offer.id, nas_b.id)

    reconcile_detected_outages(db_session, now=NOW)
    incidents = _classifier_incidents(db_session)
    assert len(incidents) == 1
    assert incidents[0].root_node_id == b.id  # deepest dark

    # B recovers, A stays dark -> deepest dark moves up to A.
    b.live_status = "up"
    db_session.flush()
    counters = reconcile_detected_outages(db_session, now=NOW + timedelta(seconds=30))

    incidents = _classifier_incidents(db_session)
    assert len(incidents) == 1  # re-pointed, NOT duplicated
    assert incidents[0].root_node_id == a.id
    assert counters["rerooted"] == 1
    assert counters["suspected_opened"] == 0


# --- idempotency + single-flight ------------------------------------------


def test_rerun_is_idempotent_no_duplicate(db_session, catalog_offer):
    _dark_nas_node(db_session, catalog_offer.id, "edge-idem", 3)
    reconcile_detected_outages(db_session, now=NOW)
    counters = reconcile_detected_outages(db_session, now=NOW)  # same instant
    assert len(_classifier_incidents(db_session)) == 1
    assert counters["suspected_opened"] == 0


def test_overlapping_reconcile_is_skipped():
    from contextlib import contextmanager
    from unittest import mock

    from app.tasks import topology_outage

    @contextmanager
    def held_lock(key, timeout_ms=None):
        yield (None, False)

    with mock.patch.object(
        topology_outage.db_session_adapter, "advisory_lock", held_lock
    ):
        assert topology_outage.reconcile_detected_outages() == {
            "skipped": "already_running"
        }


# --- operator incidents are untouched --------------------------------------


def test_operator_incidents_unaffected(db_session, catalog_offer):
    # An operator open incident on an unrelated basestation-less node.
    nas = _nas(db_session, "NAS-OP", "10.2.0.1")
    op_node = _node(db_session, "op-node", mid=nas.id, live_status="up")
    op = declare_outage(db_session, node=op_node, declared_by="noc@x")
    assert op.status == "open"
    assert op.detection_source == "operator"

    # A separate classifier outage elsewhere.
    _dark_nas_node(db_session, catalog_offer.id, "edge-op", 25)
    reconcile_detected_outages(db_session, now=NOW)

    refreshed = db_session.get(OutageIncident, op.id)
    assert refreshed.status == "open"  # untouched by the classifier loop
    assert refreshed.detection_source == "operator"
    assert refreshed.suspected_at is None and refreshed.confirmed_at is None
    # Still surfaced as open, alongside the classifier incident.
    open_ids = {i.id for i in list_open_incidents(db_session)}
    assert op.id in open_ids
