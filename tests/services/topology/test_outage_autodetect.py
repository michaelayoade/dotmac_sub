"""Outage auto-detection evaluator (Phase 5b): transitions, suppression,
thresholds, idempotency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import CPEDevice, DeviceStatus
from app.models.network_monitoring import (
    DeviceRole,
    NetworkDevice,
    NetworkTopologyLink,
    OutageIncident,
    PopSite,
)
from app.models.subscriber import Subscriber
from app.services.topology.customer_path import CustomerPath
from app.services.topology.outage import (
    AUTO_DETECT_ACTOR,
    AUTO_NOTE_PREFIX,
    declare_outage,
    detection_source,
    open_incident_for_path,
)
from app.services.topology.outage_autodetect import (
    evaluate_outages,
    radio_snapshot,
)

NOW = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)


def _dev(db, name, *, role=DeviceRole.edge, live_status="up", status_at=None, pop=None):
    d = NetworkDevice(
        name=name,
        role=role,
        is_active=True,
        live_status=live_status,
        live_status_at=status_at,
        pop_site_id=pop.id if pop is not None else None,
    )
    db.add(d)
    db.flush()
    return d


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


def _radio(db, ap, offer_id, *, uisp_status="active", tag=""):
    """Subscriber-linked active radio parented to ``ap`` + active subscription."""
    sub = Subscriber(
        first_name="R", last_name=tag or "X", email=f"r{tag}-{ap.id}@ex.com"
    )
    db.add(sub)
    db.flush()
    db.add(
        Subscription(
            subscriber_id=sub.id,
            offer_id=offer_id,
            status=SubscriptionStatus.active,
        )
    )
    cpe = CPEDevice(
        subscriber_id=sub.id,
        parent_network_device_id=ap.id,
        status=DeviceStatus.active,
        last_uisp_status=uisp_status,
    )
    db.add(cpe)
    db.flush()
    return cpe


def _baseline_all_up(db):
    """A previous-run snapshot claiming every current radio was up."""
    baseline = radio_snapshot(db)
    for entry in baseline.values():
        entry["up"] = True
    return baseline


def _open_incidents(db):
    return db.query(OutageIncident).filter(OutageIncident.status == "open").all()


def test_router_down_yields_one_incident_radios_suppressed(db_session, catalog_offer):
    """Router down + AP down behind it + 8 radios gone -> ONE auto incident at
    the router; the AP's down event is suppressed as unreachable_upstream."""
    core = _dev(db_session, "Core", role=DeviceRole.core)
    router = _dev(db_session, "Router", live_status="down", status_at=NOW)
    ap = _dev(db_session, "AP", live_status="down", status_at=NOW)
    _link(db_session, core, router)
    _link(db_session, router, ap)
    for i in range(8):
        _radio(db_session, ap, catalog_offer.id, uisp_status="disconnected", tag=str(i))
    baseline = _baseline_all_up(db_session)

    counters, _ = evaluate_outages(db_session, now=NOW, radio_baseline=baseline)

    assert counters["suppressed_unreachable"] == 1  # the AP
    assert counters["incidents_created"] == 1
    incidents = _open_incidents(db_session)
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident.root_node_id == router.id  # root cause, not the AP
    assert incident.declared_by == AUTO_DETECT_ACTOR
    assert detection_source(incident) == "auto"
    assert incident.note.startswith(AUTO_NOTE_PREFIX)
    assert incident.affected_count == 8


def test_ap_scope_trip_creates_one_incident_at_the_ap(db_session, catalog_offer):
    """4 of 5 radios newly gone on a healthy AP -> proactive sector-down
    incident scoped to the AP."""
    ap = _dev(db_session, "AP-Sector-2")
    radios = [
        _radio(db_session, ap, catalog_offer.id, uisp_status="active", tag=str(i))
        for i in range(5)
    ]
    baseline = _baseline_all_up(db_session)
    for cpe in radios[:4]:
        cpe.last_uisp_status = "disconnected"
    db_session.flush()

    counters, _ = evaluate_outages(db_session, now=NOW, radio_baseline=baseline)

    assert counters["events_seen"] == 4
    assert counters["incidents_created"] == 1
    incident = _open_incidents(db_session)[0]
    assert incident.root_node_id == ap.id
    assert "AP-Sector-2" in incident.note


def test_below_min_affected_creates_nothing(db_session, catalog_offer):
    ap = _dev(db_session, "AP")
    radios = [_radio(db_session, ap, catalog_offer.id, tag=str(i)) for i in range(10)]
    baseline = _baseline_all_up(db_session)
    for cpe in radios[:2]:  # 2 < min_affected (3)
        cpe.last_uisp_status = "disconnected"
    db_session.flush()

    counters, _ = evaluate_outages(db_session, now=NOW, radio_baseline=baseline)
    assert counters["events_seen"] == 2
    assert counters["incidents_created"] == 0
    assert _open_incidents(db_session) == []


def test_below_min_fraction_creates_nothing(db_session, catalog_offer):
    """3 of 10 passes min_affected but not the 40% fraction gate — normal
    churn-level noise on a big sector."""
    ap = _dev(db_session, "AP")
    radios = [_radio(db_session, ap, catalog_offer.id, tag=str(i)) for i in range(10)]
    baseline = _baseline_all_up(db_session)
    for cpe in radios[:3]:
        cpe.last_uisp_status = "disconnected"
    db_session.flush()

    counters, _ = evaluate_outages(db_session, now=NOW, radio_baseline=baseline)
    assert counters["incidents_created"] == 0


def test_tiny_denominator_cannot_trip(db_session, catalog_offer):
    """2 of 2 radios gone is 100% but below min_affected — a two-customer
    sector never auto-declares."""
    ap = _dev(db_session, "AP")
    radios = [_radio(db_session, ap, catalog_offer.id, tag=str(i)) for i in range(2)]
    baseline = _baseline_all_up(db_session)
    for cpe in radios:
        cpe.last_uisp_status = "disconnected"
    db_session.flush()

    counters, _ = evaluate_outages(db_session, now=NOW, radio_baseline=baseline)
    assert counters["incidents_created"] == 0


def test_chronic_offline_is_not_an_event(db_session, catalog_offer):
    """Down before the window (infra) / down in the baseline (radios) is
    churn, not a transition — absolute-state logic would drown here."""
    core = _dev(db_session, "Core", role=DeviceRole.core)
    router = _dev(
        db_session,
        "Router",
        live_status="down",
        status_at=NOW - timedelta(hours=2),  # entered down long before window
    )
    _link(db_session, core, router)
    ap = _dev(db_session, "AP")
    for i in range(5):
        _radio(db_session, ap, catalog_offer.id, uisp_status="disconnected", tag=str(i))
    baseline = radio_snapshot(db_session)  # baseline already has them down

    counters, _ = evaluate_outages(db_session, now=NOW, radio_baseline=baseline)
    assert counters["events_seen"] == 0
    assert counters["incidents_created"] == 0
    assert _open_incidents(db_session) == []


def test_first_run_seeds_baseline_without_events(db_session, catalog_offer):
    ap = _dev(db_session, "AP")
    for i in range(5):
        _radio(db_session, ap, catalog_offer.id, uisp_status="disconnected", tag=str(i))

    counters, snapshot = evaluate_outages(db_session, now=NOW, radio_baseline=None)
    assert counters["events_seen"] == 0
    assert counters["incidents_created"] == 0
    assert len(snapshot) == 5
    assert all(not entry["up"] for entry in snapshot.values())


def test_open_incident_prevents_duplicate(db_session, catalog_offer):
    core = _dev(db_session, "Core", role=DeviceRole.core)
    router = _dev(db_session, "Router", live_status="down", status_at=NOW)
    ap = _dev(db_session, "AP", live_status="down", status_at=NOW)
    _link(db_session, core, router)
    _link(db_session, router, ap)
    for i in range(4):
        _radio(db_session, ap, catalog_offer.id, uisp_status="disconnected", tag=str(i))
    baseline = _baseline_all_up(db_session)
    declare_outage(db_session, node=router, declared_by="noc@x", note="fiber cut")

    counters, _ = evaluate_outages(db_session, now=NOW, radio_baseline=baseline)
    assert counters["incidents_created"] == 0
    assert counters["skipped_open_incident"] >= 1
    assert len(_open_incidents(db_session)) == 1  # still just the manual one


def test_rerun_is_idempotent(db_session, catalog_offer):
    """The same ongoing outage must not spawn duplicates on the next scan."""
    ap = _dev(db_session, "AP")
    radios = [_radio(db_session, ap, catalog_offer.id, tag=str(i)) for i in range(5)]
    baseline = _baseline_all_up(db_session)
    for cpe in radios[:4]:
        cpe.last_uisp_status = "disconnected"
    db_session.flush()

    first, _ = evaluate_outages(db_session, now=NOW, radio_baseline=baseline)
    assert first["incidents_created"] == 1
    # Next run, even against the stale baseline (worst case for duplication):
    second, _ = evaluate_outages(db_session, now=NOW, radio_baseline=baseline)
    assert second["incidents_created"] == 0
    assert second["skipped_open_incident"] >= 1
    assert len(_open_incidents(db_session)) == 1


def test_root_event_below_subscriber_impact_is_noise(db_session):
    """A down device serving nobody never opens an incident (min_affected on
    downstream subscriber impact)."""
    core = _dev(db_session, "Core", role=DeviceRole.core)
    router = _dev(db_session, "Router", live_status="down", status_at=NOW)
    _link(db_session, core, router)

    counters, _ = evaluate_outages(db_session, now=NOW, radio_baseline=None)
    assert counters["events_seen"] == 1
    assert counters["incidents_created"] == 0


def test_cosited_root_causes_collapse_to_basestation(db_session, catalog_offer):
    """Two independent root causes at one pop_site = a site problem — ONE
    basestation incident, not two node incidents."""
    pop = PopSite(name="Garki", zabbix_group_id="10")
    db_session.add(pop)
    db_session.flush()
    core = _dev(db_session, "Core", role=DeviceRole.core)
    sw1 = _dev(db_session, "SW1", live_status="down", status_at=NOW, pop=pop)
    sw2 = _dev(db_session, "SW2", live_status="down", status_at=NOW, pop=pop)
    _link(db_session, core, sw1)
    _link(db_session, core, sw2)
    for i in range(3):
        _radio(db_session, sw1, catalog_offer.id, tag=f"a{i}")
        _radio(db_session, sw2, catalog_offer.id, tag=f"b{i}")

    counters, _ = evaluate_outages(db_session, now=NOW, radio_baseline=None)
    assert counters["incidents_created"] == 1
    incident = _open_incidents(db_session)[0]
    assert incident.basestation_id == pop.id
    assert incident.root_node_id is None


def test_auto_incident_lights_up_customer_known_outage_flag(db_session, catalog_offer):
    """The /me connection endpoint's known_outage flag reads
    open_incident_for_path — auto-detected incidents must match it with no
    extra plumbing."""
    ap = _dev(db_session, "AP")
    radios = [_radio(db_session, ap, catalog_offer.id, tag=str(i)) for i in range(4)]
    baseline = _baseline_all_up(db_session)
    for cpe in radios:
        cpe.last_uisp_status = "disconnected"
    db_session.flush()

    counters, _ = evaluate_outages(db_session, now=NOW, radio_baseline=baseline)
    assert counters["incidents_created"] == 1

    incident = open_incident_for_path(db_session, CustomerPath(node=ap))
    assert incident is not None
    assert detection_source(incident) == "auto"


def test_multi_candidate_scan_walks_graph_once(db_session, catalog_offer, monkeypatch):
    """A cascading failure with several simultaneous candidates must reuse ONE
    precomputed adjacency + dist-to-core across classification, impact gates,
    open-incident checks and declares — not one full-graph BFS (of per-node
    queries) per candidate."""
    import app.services.topology.affected as affected_mod
    import app.services.topology.outage as outage_mod
    import app.services.topology.outage_autodetect as autodetect_mod
    import app.services.topology.reachability as reachability_mod

    core = _dev(db_session, "Core", role=DeviceRole.core)
    for r in range(3):
        router = _dev(db_session, f"R{r}", live_status="down", status_at=NOW)
        _link(db_session, core, router)
        for i in range(3):
            _radio(db_session, router, catalog_offer.id, tag=f"{r}-{i}")

    calls = {"dist": 0, "adjacency": 0}
    real_dist = affected_mod._dist_to_core
    real_adjacency = affected_mod.lldp_adjacency

    def dist_spy(session, **kwargs):
        calls["dist"] += 1
        return real_dist(session, **kwargs)

    def adjacency_spy(session):
        calls["adjacency"] += 1
        return real_adjacency(session)

    for mod in (affected_mod, outage_mod, autodetect_mod):
        monkeypatch.setattr(mod, "_dist_to_core", dist_spy)
    for mod in (affected_mod, outage_mod, autodetect_mod, reachability_mod):
        monkeypatch.setattr(mod, "lldp_adjacency", adjacency_spy)

    counters, _ = evaluate_outages(db_session, now=NOW, radio_baseline=None)

    assert counters["incidents_created"] == 3  # three independent root causes
    assert calls == {"dist": 1, "adjacency": 1}
