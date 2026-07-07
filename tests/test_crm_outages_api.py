"""CRM outage API: list + detail (read-only consumption for CRM/mobile)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.api import crm as crm_routes
from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network_monitoring import NetworkDevice, PopSite
from app.models.subscriber import Subscriber
from app.services import crm_api
from app.services.topology.outage import (
    AUTO_DETECT_ACTOR,
    confirm_incident,
    declare_outage,
    discard_incident,
    open_classifier_incident,
    resolve_classifier_incident,
    resolve_outage,
    start_clearing,
)


def _nas_node_with_subs(db, offer_id, n, *, pop=None):
    nas = NasDevice(name=f"NAS-{uuid.uuid4().hex[:6]}", management_ip=f"10.9.{n}.1")
    db.add(nas)
    db.flush()
    node = NetworkDevice(
        name=f"node-{uuid.uuid4().hex[:6]}",
        matched_device_type="nas",
        matched_device_id=nas.id,
        pop_site_id=pop.id if pop is not None else None,
        is_active=True,
    )
    db.add(node)
    db.flush()
    for i in range(n):
        s = Subscriber(
            first_name="Ada",
            last_name=f"L{i}",
            email=f"{i}-{nas.id}@ex.com",
            city="Abuja",
        )
        db.add(s)
        db.flush()
        db.add(
            Subscription(
                subscriber_id=s.id,
                offer_id=offer_id,
                status=SubscriptionStatus.active,
                provisioning_nas_device_id=nas.id,
            )
        )
    db.flush()
    return node


def test_list_serializes_scope_and_detection_source(db_session, catalog_offer):
    node = _nas_node_with_subs(db_session, catalog_offer.id, 2)
    manual = declare_outage(db_session, node=node, declared_by="noc@x", note="cut")
    auto = declare_outage(db_session, node=node, declared_by=AUTO_DETECT_ACTOR)

    rows, total = crm_api.list_outage_incidents(db_session)
    assert total == 2
    by_id = {r["id"]: r for r in rows}
    # detection_source stays the legacy auto/manual field (backward-compatible);
    # provenance is the new operator/classifier discriminator.
    assert by_id[str(manual.id)]["detection_source"] == "manual"
    assert by_id[str(auto.id)]["detection_source"] == "auto"
    assert by_id[str(manual.id)]["provenance"] == "operator"
    assert by_id[str(auto.id)]["provenance"] == "operator"
    row = by_id[str(manual.id)]
    assert row["scope"]["type"] == "node"
    assert row["scope"]["name"] == node.name
    assert row["status"] == "open"
    assert row["state"] == "open"
    assert row["affected_count"] == 2
    assert row["started_at"] is not None
    assert row["resolved_at"] is None
    assert row["mttr_seconds"] is None


def test_list_includes_recently_resolved_but_not_stale(db_session, catalog_offer):
    node = _nas_node_with_subs(db_session, catalog_offer.id, 1)
    open_inc = declare_outage(db_session, node=node)
    recent = declare_outage(db_session, node=node)
    resolve_outage(db_session, recent.id)
    old = declare_outage(db_session, node=node)
    resolve_outage(db_session, old.id)
    old.resolved_at = datetime.now(UTC) - timedelta(days=3)
    db_session.flush()

    rows, _ = crm_api.list_outage_incidents(db_session)
    ids = {r["id"] for r in rows}
    assert str(open_inc.id) in ids
    assert str(recent.id) in ids
    assert str(old.id) not in ids  # resolved past the recency window

    resolved_rows, _ = crm_api.list_outage_incidents(db_session, status="resolved")
    assert {r["id"] for r in resolved_rows} == {str(recent.id), str(old.id)}


def test_default_active_view_includes_confirmed_classifier_excludes_suspected(
    db_session, catalog_offer
):
    """§7.6 finding 4: the default CRM list surfaces ACTIVE classifier incidents
    (confirmed/clearing, resolved_at NULL) alongside operator ``open`` — but a
    still-debouncing ``suspected`` incident is noise and stays hidden, and a
    ``discarded`` false positive never shows."""
    node = _nas_node_with_subs(db_session, catalog_offer.id, 2)
    now = datetime.now(UTC)

    op_open = declare_outage(db_session, node=node, declared_by="noc@x")

    confirmed = open_classifier_incident(
        db_session, root_node=node, affected_count=2, confidence=0.9, now=now
    )
    confirm_incident(db_session, confirmed, now=now)

    clearing = open_classifier_incident(
        db_session, root_node=node, affected_count=1, now=now
    )
    confirm_incident(db_session, clearing, now=now)
    start_clearing(db_session, clearing, now=now)

    suspected = open_classifier_incident(db_session, root_node=node, now=now)

    discarded = open_classifier_incident(db_session, root_node=node, now=now)
    discard_incident(db_session, discarded)
    db_session.flush()

    rows, _ = crm_api.list_outage_incidents(db_session)
    ids = {r["id"] for r in rows}
    assert str(op_open.id) in ids
    assert str(confirmed.id) in ids  # debounced-real → visible
    assert str(clearing.id) in ids  # still settling → visible
    assert str(suspected.id) not in ids  # not yet confirmed → hidden noise
    assert str(discarded.id) not in ids  # false positive → hidden

    by_id = {r["id"]: r for r in rows}
    conf_row = by_id[str(confirmed.id)]
    assert conf_row["provenance"] == "classifier"
    # legacy field is auto/manual only; a classifier row (declared_by=CLASSIFIER_ACTOR)
    # reports "manual" there — accurate provenance lives in the new field.
    assert conf_row["detection_source"] == "manual"
    assert conf_row["state"] == "confirmed"
    assert conf_row["confirmed_at"] is not None
    assert conf_row["confidence"] == 0.9
    assert conf_row["mttr_seconds"] is None  # not resolved yet


def test_row_carries_mttr_and_state_for_resolved_classifier(db_session, catalog_offer):
    """A resolved classifier incident carries MTTR (resolved_at - confirmed_at)
    and the terminal ``resolved`` state; narrowing by status='suspected' works."""
    node = _nas_node_with_subs(db_session, catalog_offer.id, 1)
    confirmed_at = datetime.now(UTC) - timedelta(minutes=30)
    inc = open_classifier_incident(db_session, root_node=node, now=confirmed_at)
    confirm_incident(db_session, inc, now=confirmed_at)
    start_clearing(db_session, inc, now=confirmed_at + timedelta(minutes=20))
    resolve_classifier_incident(
        db_session, inc, now=confirmed_at + timedelta(minutes=30)
    )
    db_session.flush()

    # Narrow to the terminal state to fetch it (excluded from the active default).
    rows, _ = crm_api.list_outage_incidents(db_session, status="resolved")
    row = next(r for r in rows if r["id"] == str(inc.id))
    assert row["state"] == "resolved"
    assert row["provenance"] == "classifier"
    assert row["mttr_seconds"] == 30 * 60

    # A suspected incident is reachable only via explicit narrowing.
    susp = open_classifier_incident(db_session, root_node=node, now=datetime.now(UTC))
    db_session.flush()
    susp_rows, _ = crm_api.list_outage_incidents(db_session, status="suspected")
    assert {r["id"] for r in susp_rows} == {str(susp.id)}


def test_list_filters_by_basestation_scope(db_session):
    pop = PopSite(name="Garki", zabbix_group_id="10")
    other = PopSite(name="Wuse", zabbix_group_id="11")
    db_session.add_all([pop, other])
    db_session.flush()
    match = declare_outage(db_session, basestation=pop)
    declare_outage(db_session, basestation=other)

    rows, total = crm_api.list_outage_incidents(db_session, basestation_id=str(pop.id))
    assert total == 1
    assert rows[0]["id"] == str(match.id)
    assert rows[0]["scope"] == {
        "type": "basestation",
        "id": str(pop.id),
        "name": "Garki",
        "basestation_id": str(pop.id),
    }


def test_detail_lists_affected_subscriptions(db_session, catalog_offer):
    node = _nas_node_with_subs(db_session, catalog_offer.id, 3)
    incident = declare_outage(db_session, node=node)

    detail = crm_api.outage_incident_detail(db_session, str(incident.id))
    assert detail is not None
    assert detail["affected_total"] == 3
    assert detail["affected_truncated"] is False
    entry = detail["affected_subscriptions"][0]
    assert entry["subscriber_name"].startswith("Ada")
    assert entry["status"] == "active"
    assert entry["service_address"]  # falls back to the subscriber address
    assert entry["subscription_id"]


def test_detail_caps_affected_list(db_session, catalog_offer):
    """More affected subscriptions than ``limit``: the page is sliced BEFORE
    hydration (bounded work on a big outage), totals stay honest, and the
    sliced entries are still fully hydrated (eager-loaded page query)."""
    node = _nas_node_with_subs(db_session, catalog_offer.id, 4)
    incident = declare_outage(db_session, node=node)

    detail = crm_api.outage_incident_detail(db_session, str(incident.id), limit=2)
    assert detail["affected_total"] == 4
    assert detail["affected_truncated"] is True
    assert len(detail["affected_subscriptions"]) == 2
    for entry in detail["affected_subscriptions"]:
        assert entry["subscriber_name"]
        assert entry["service_address"]
        assert entry["status"] == "active"


def test_detail_unknown_incident_is_none_and_route_404s(db_session):
    assert crm_api.outage_incident_detail(db_session, str(uuid.uuid4())) is None
    with pytest.raises(HTTPException) as exc:
        crm_routes.outage_detail(str(uuid.uuid4()), db=db_session)
    assert exc.value.status_code == 404


def test_list_route_returns_envelope_and_validates_status(db_session, catalog_offer):
    node = _nas_node_with_subs(db_session, catalog_offer.id, 1)
    declare_outage(db_session, node=node)

    resp = crm_routes.list_outages(db=db_session)
    assert "data" in resp and resp["meta"]["total"] == 1

    with pytest.raises(HTTPException) as exc:
        crm_routes.list_outages(status_filter="bogus", db=db_session)
    assert exc.value.status_code == 400


def test_detail_route_returns_envelope(db_session, catalog_offer):
    node = _nas_node_with_subs(db_session, catalog_offer.id, 1)
    incident = declare_outage(db_session, node=node)
    resp = crm_routes.outage_detail(str(incident.id), db=db_session)
    assert resp["data"]["id"] == str(incident.id)


def test_outage_routes_declare_crm_bearer_guard():
    """Mirror of the route-guard convention: the new CRM outage routes must
    carry the same bearer dependency as the rest of the /crm surface."""
    from app.api.crm import router

    seen = set()
    for route in router.routes:
        if getattr(route, "path", "") in ("/crm/outages", "/crm/outages/{incident_id}"):
            names = {
                getattr(dep.dependency, "__name__", "")
                for dep in getattr(route, "dependencies", [])
            }
            assert "require_crm_bearer" in names
            seen.add(route.path)
    assert seen == {"/crm/outages", "/crm/outages/{incident_id}"}
