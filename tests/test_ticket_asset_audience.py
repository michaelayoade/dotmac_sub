from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.network import (
    CPEDevice,
    FiberAccessPoint,
    FiberTerminationPoint,
    ODNEndpointType,
)
from app.models.network_monitoring import DeviceType, NetworkDevice, PopSite
from app.schemas.infrastructure import TicketLocationReference, TicketLocationSource
from app.services.fiber_topology import (
    FiberSubscriptionTrace,
    FiberTraceGap,
    FiberTraceHop,
)
from app.services.network import outage_impact


def _fiber_asset(db_session):
    access_point = FiberAccessPoint(name=f"FAP-{uuid4()}", is_active=True)
    db_session.add(access_point)
    db_session.flush()
    termination = FiberTerminationPoint(
        endpoint_type=ODNEndpointType.fiber_access_point,
        ref_id=access_point.id,
        is_active=True,
    )
    db_session.add(termination)
    db_session.commit()
    return access_point, termination


def _trace(subscription_id, termination_id, *, complete: bool):
    return FiberSubscriptionTrace(
        subscription_id=subscription_id,
        customer_label="Customer",
        subscription_status="active",
        hops=(
            FiberTraceHop(
                kind="termination",
                label="Fiber access point",
                asset_id=termination_id,
                evidence="reviewed connection route",
            ),
        ),
        gaps=(
            ()
            if complete
            else (FiberTraceGap(code="route_missing", message="Route incomplete"),)
        ),
        electronic_complete=True,
        physical_complete=complete,
        upstream_scope="pop_boundary_only",
        upstream_message="",
    )


def test_fiber_audience_uses_verified_subscription_route(
    db_session, active_subscription, monkeypatch
):
    access_point, termination = _fiber_asset(db_session)
    monkeypatch.setattr(
        outage_impact.infrastructure_catalogue,
        "resolve_ticket_location",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        outage_impact,
        "trace_fiber_subscription",
        lambda _db, subscription_id: _trace(
            subscription_id, termination.id, complete=True
        ),
    )

    result = outage_impact.resolve_ticket_asset_audience(
        db_session,
        query=outage_impact.TicketAssetAudienceQuery(
            asset=TicketLocationReference(
                source=TicketLocationSource.fiber_access_point,
                id=access_point.id,
            )
        ),
    )

    assert result.subscription_ids == (active_subscription.id,)
    assert result.affected_count == 1
    assert result.scope_token


def test_base_station_audience_rechecks_each_customer_path(
    db_session, active_subscription, monkeypatch
):
    site = PopSite(name=f"BTS-{uuid4()}", zabbix_group_id=uuid4().hex[:20])
    db_session.add(site)
    db_session.commit()
    monkeypatch.setattr(
        outage_impact.infrastructure_catalogue,
        "resolve_ticket_location",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        outage_impact,
        "affected_customers",
        lambda *_args, **_kwargs: {
            "node_ids": {uuid4()},
            "subscriptions": [active_subscription],
        },
    )
    monkeypatch.setattr(
        outage_impact,
        "resolve_customer_path",
        lambda *_args, **_kwargs: SimpleNamespace(
            gap=None, basestation=site, radio_resolution=None, radio=None
        ),
    )

    result = outage_impact.resolve_ticket_asset_audience(
        db_session,
        query=outage_impact.TicketAssetAudienceQuery(
            asset=TicketLocationReference(
                source=TicketLocationSource.base_station, id=site.id
            )
        ),
    )

    assert result.subscription_ids == (active_subscription.id,)


def test_fiber_audience_stops_if_any_active_route_is_incomplete(
    db_session, active_subscription, monkeypatch
):
    access_point, termination = _fiber_asset(db_session)
    monkeypatch.setattr(
        outage_impact.infrastructure_catalogue,
        "resolve_ticket_location",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        outage_impact,
        "trace_fiber_subscription",
        lambda _db, subscription_id: _trace(
            subscription_id, termination.id, complete=False
        ),
    )

    with pytest.raises(outage_impact.TicketAssetAudienceError) as exc:
        outage_impact.resolve_ticket_asset_audience(
            db_session,
            query=outage_impact.TicketAssetAudienceQuery(
                asset=TicketLocationReference(
                    source=TicketLocationSource.fiber_access_point,
                    id=access_point.id,
                )
            ),
        )

    assert exc.value.code == "network.outage_impact.fiber_route_incomplete"


def test_wireless_audience_rejects_unbound_customer_radio(
    db_session, subscriber, monkeypatch
):
    node = NetworkDevice(
        name=f"AP-{uuid4()}", device_type=DeviceType.access_point, is_active=True
    )
    db_session.add(node)
    db_session.flush()
    db_session.add(
        CPEDevice(
            subscriber_id=subscriber.id,
            parent_network_device_id=node.id,
            subscription_id=None,
            uisp_synced_at=datetime.now(UTC),
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        outage_impact.infrastructure_catalogue,
        "resolve_ticket_location",
        lambda *_args, **_kwargs: object(),
    )

    with pytest.raises(outage_impact.TicketAssetAudienceError) as exc:
        outage_impact.resolve_ticket_asset_audience(
            db_session,
            query=outage_impact.TicketAssetAudienceQuery(
                asset=TicketLocationReference(
                    source=TicketLocationSource.wireless_access_point,
                    id=node.id,
                )
            ),
        )

    assert exc.value.code == "network.outage_impact.radio_subscription_missing"
