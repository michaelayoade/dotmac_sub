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

import pytest

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network_monitoring import (
    DeviceRole,
    NetworkDevice,
    NetworkTopologyLink,
    OutageIncident,
    PopSite,
)
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Subscriber
from app.services.topology import outage as outage_svc
from app.services.topology.outage import (
    CLASSIFIER_SOURCE,
    declare_outage,
    list_open_incidents,
    list_operator_open_incidents,
    open_classifier_incident,
    resolve_outage,
    set_outage_status,
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


# --- finding 1: one basestation = one incident within a pass ---------------


def test_two_dark_components_same_basestation_open_one_incident(
    db_session, catalog_offer
):
    # Two DISTINCT (unlinked) dark NAS nodes under the SAME basestation -> two
    # boundaries, one incident (identity is the site), affected_count summed.
    pop = PopSite(name="BTS-1", zabbix_group_id="10")
    nas_a = _nas(db_session, "NAS-C1", "10.5.0.1")
    nas_b = _nas(db_session, "NAS-C2", "10.5.0.2")
    db_session.add(pop)
    db_session.flush()
    _node(db_session, "c1", mid=nas_a.id, live_status="down", pop=pop)
    _node(db_session, "c2", mid=nas_b.id, live_status="down", pop=pop)
    for _ in range(4):
        _sub(db_session, catalog_offer.id, nas_a.id)
    for _ in range(3):
        _sub(db_session, catalog_offer.id, nas_b.id)

    counters = reconcile_detected_outages(db_session, now=NOW)

    incidents = _classifier_incidents(db_session)
    assert len(incidents) == 1  # NOT two — merged by basestation
    assert counters["suspected_opened"] == 1
    inc = incidents[0]
    assert inc.basestation_id == pop.id
    assert inc.affected_count == 7  # 4 + 3 across both components


# --- finding 2: suspicion sustained past W_confirm confirms, not discards ---


def test_recovery_past_confirm_window_confirms_not_discards(
    db_session, catalog_offer, monkeypatch
):
    kinds = _capture_events(monkeypatch)
    node, nas = _dark_nas_node(db_session, catalog_offer.id, "edge-sustained", 3)
    reconcile_detected_outages(db_session, now=NOW)  # suspected (600s window)
    assert _classifier_incidents(db_session)[0].status == "suspected"

    # It stayed dark PAST W_confirm (600s) but no pass ran in between; then it
    # recovers -> must confirm (preserving MTTR) + start clearing, NOT discard.
    node.live_status = "up"
    db_session.flush()
    counters = reconcile_detected_outages(db_session, now=NOW + timedelta(seconds=650))

    inc = _classifier_incidents(db_session)[0]
    assert inc.status == "clearing"
    assert inc.confirmed_at is not None  # MTTR anchor preserved
    assert inc.cleared_at is not None
    assert counters["confirmed"] == 1 and counters["clearing"] == 1
    assert counters["discarded"] == 0
    assert "outage.confirmed" in kinds and "outage.discarded" not in kinds

    # And it resolves after W_resolve (300s), giving a positive MTTR.
    reconcile_detected_outages(db_session, now=NOW + timedelta(seconds=650 + 301))
    inc = _classifier_incidents(db_session)[0]
    assert inc.status == "resolved"
    assert (inc.resolved_at - inc.confirmed_at).total_seconds() > 0


def test_recovery_before_confirm_window_still_discards(db_session, catalog_offer):
    # Control: recovered BEFORE W_confirm is still a false positive -> discarded.
    node, nas = _dark_nas_node(db_session, catalog_offer.id, "edge-blip", 3)
    reconcile_detected_outages(db_session, now=NOW)  # suspected (600s window)
    node.live_status = "up"
    db_session.flush()
    counters = reconcile_detected_outages(db_session, now=NOW + timedelta(seconds=60))
    inc = _classifier_incidents(db_session)[0]
    assert inc.status == "discarded"
    assert counters["discarded"] == 1 and counters["confirmed"] == 0


# --- finding 3: operator console / resolve never touch classifier rows ------


def test_operator_resolve_is_noop_on_classifier_incident(db_session, catalog_offer):
    nas = _nas(db_session, "NAS-CL", "10.6.0.1")
    node = _node(db_session, "cl", mid=nas.id, live_status="down")
    inc = open_classifier_incident(
        db_session,
        root_node=node,
        affected_count=3,
        classification="node_outage",
        now=NOW,
    )
    assert inc.status == "suspected"

    result = resolve_outage(db_session, inc.id)  # operator Resolve button path
    assert result.status == "suspected"  # untouched
    assert result.resolved_at is None
    # The low-level writer refuses classifier incidents outright.
    with pytest.raises(ValueError):
        set_outage_status(inc, "resolved")


def test_operator_console_listing_excludes_classifier(db_session, catalog_offer):
    # One operator open incident + one classifier incident.
    op_nas = _nas(db_session, "NAS-OPC", "10.6.1.1")
    op_node = _node(db_session, "opc", mid=op_nas.id, live_status="up")
    op = declare_outage(db_session, node=op_node, declared_by="noc@x")
    _dark_nas_node(db_session, catalog_offer.id, "edge-cl", 25)
    reconcile_detected_outages(db_session, now=NOW)

    operator_ids = {i.id for i in list_operator_open_incidents(db_session)}
    assert op.id in operator_ids
    # No classifier incident leaks into the operator console listing.
    classifier_ids = {i.id for i in _classifier_incidents(db_session)}
    assert operator_ids.isdisjoint(classifier_ids)
    assert all(
        i.detection_source == "operator"
        for i in list_operator_open_incidents(db_session)
    )
