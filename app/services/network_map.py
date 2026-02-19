from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.domain_settings import SettingDomain
from app.models.network import FiberAccessPoint, FiberSegment, FiberSpliceClosure, FdhCabinet, Splitter
from app.models.network_monitoring import NetworkDevice, PopSite
from app.models.subscriber import Address, Subscriber
from app.models.usage import AccountingStatus, RadiusAccountingSession
from app.services import settings_spec


def build_network_map_context(db: Session) -> dict:
    features: list[dict] = []

    # POP Sites
    pop_sites = (
        db.query(PopSite)
        .filter(PopSite.is_active.is_(True))
        .filter(PopSite.latitude.isnot(None))
        .filter(PopSite.longitude.isnot(None))
        .all()
    )
    pop_ids = [site.id for site in pop_sites]
    pop_device_counts = {}
    if pop_ids:
        pop_device_counts = {
            row[0]: row[1]
            for row in db.query(NetworkDevice.pop_site_id, func.count(NetworkDevice.id))
            .filter(NetworkDevice.pop_site_id.in_(pop_ids))
            .filter(NetworkDevice.is_active.is_(True))
            .group_by(NetworkDevice.pop_site_id)
            .all()
        }
    for pop in pop_sites:
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [pop.longitude, pop.latitude]},
                "properties": {
                    "id": str(pop.id),
                "type": "pop_site",
                "name": pop.name,
                "code": pop.code,
                "city": pop.city,
                "device_count": pop_device_counts.get(pop.id, 0),
            },
        }
        )

    # FDH Cabinets
    fdhs = (
        db.query(FdhCabinet)
        .filter(FdhCabinet.is_active.is_(True))
        .filter(FdhCabinet.latitude.isnot(None))
        .filter(FdhCabinet.longitude.isnot(None))
        .all()
    )
    splitter_counts: dict[UUID | None, int] = {}
    if fdhs:
        fdh_ids = [fdh.id for fdh in fdhs]
        splitter_counts = {
            row[0]: row[1]
            for row in db.query(Splitter.fdh_id, func.count(Splitter.id))
            .filter(Splitter.fdh_id.in_(fdh_ids))
            .group_by(Splitter.fdh_id)
            .all()
        }
    for fdh in fdhs:
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [fdh.longitude, fdh.latitude]},
                "properties": {
                    "id": str(fdh.id),
                    "type": "fdh_cabinet",
                    "name": fdh.name,
                    "code": fdh.code,
                    "splitter_count": splitter_counts.get(fdh.id, 0),
                },
            }
        )

    # Splice Closures
    closures = (
        db.query(FiberSpliceClosure)
        .filter(FiberSpliceClosure.is_active.is_(True))
        .filter(FiberSpliceClosure.latitude.isnot(None))
        .filter(FiberSpliceClosure.longitude.isnot(None))
        .all()
    )
    for closure in closures:
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [closure.longitude, closure.latitude],
                },
                "properties": {
                    "id": str(closure.id),
                    "type": "splice_closure",
                    "name": closure.name,
                },
            }
        )

    # Fiber Access Points
    access_points = (
        db.query(FiberAccessPoint)
        .filter(FiberAccessPoint.is_active.is_(True))
        .filter(FiberAccessPoint.latitude.isnot(None))
        .filter(FiberAccessPoint.longitude.isnot(None))
        .all()
    )
    for ap in access_points:
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [ap.longitude, ap.latitude]},
                "properties": {
                    "id": str(ap.id),
                    "type": "access_point",
                    "name": ap.name,
                    "code": ap.code,
                    "ap_type": ap.access_point_type,
                    "placement": ap.placement,
                },
            }
        )

    # Fiber Segments
    segments = db.query(FiberSegment).filter(FiberSegment.is_active.is_(True)).all()
    segment_geoms = (
        db.query(FiberSegment, func.ST_AsGeoJSON(FiberSegment.route_geom))
        .filter(FiberSegment.is_active.is_(True), FiberSegment.route_geom.isnot(None))
        .all()
    )
    for segment, geojson_str in segment_geoms:
        if not geojson_str:
            continue
        geom = json.loads(geojson_str)
        features.append(
            {
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "id": str(segment.id),
                    "type": "fiber_segment",
                    "name": segment.name,
                    "segment_type": segment.segment_type.value
                    if segment.segment_type
                    else "distribution",
                    "cable_type": segment.cable_type.value
                    if segment.cable_type
                    else None,
                    "fiber_count": segment.fiber_count,
                    "length_m": segment.length_m,
                },
            }
        )

    # Customers with addresses that have coordinates, including online status
    active_sessions_subq = (
        db.query(Subscription.subscriber_id)
        .join(
            RadiusAccountingSession,
            RadiusAccountingSession.subscription_id == Subscription.id,
        )
        .filter(
            RadiusAccountingSession.session_end.is_(None),
            RadiusAccountingSession.status_type != AccountingStatus.stop,
        )
        .distinct()
        .subquery()
    )

    map_limit_raw = settings_spec.resolve_value(db, SettingDomain.gis, "map_customer_limit")
    try:
        map_limit = int(str(map_limit_raw)) if map_limit_raw is not None else None
    except (TypeError, ValueError):
        map_limit = None
    if map_limit is not None and map_limit <= 0:
        map_limit = None

    customer_counts = (
        db.query(
            func.count(Address.id).label("total"),
            func.sum(
                case((active_sessions_subq.c.subscriber_id.isnot(None), 1), else_=0)
            ).label("online"),
        )
        .join(Subscriber, Address.subscriber_id == Subscriber.id)
        .outerjoin(active_sessions_subq, active_sessions_subq.c.subscriber_id == Subscriber.id)
        .filter(
            Address.latitude.isnot(None),
            Address.longitude.isnot(None),
            Subscriber.is_active.is_(True),
        )
        .first()
    )

    customer_total = int(customer_counts.total or 0) if customer_counts else 0
    online_count = int(customer_counts.online or 0) if customer_counts else 0
    offline_count = max(customer_total - online_count, 0)

    customer_addresses_query = (
        db.query(
            Address.id,
            Address.address_line1,
            Address.city,
            Address.latitude,
            Address.longitude,
            Subscriber.id.label("subscriber_id"),
            Subscriber.first_name,
            Subscriber.last_name,
            (active_sessions_subq.c.subscriber_id.isnot(None)).label("is_online"),
        )
        .join(Subscriber, Address.subscriber_id == Subscriber.id)
        .outerjoin(active_sessions_subq, active_sessions_subq.c.subscriber_id == Subscriber.id)
        .filter(
            Address.latitude.isnot(None),
            Address.longitude.isnot(None),
            Subscriber.is_active.is_(True),
        )
        .order_by(Address.id)
    )
    if map_limit is not None:
        customer_addresses_query = customer_addresses_query.limit(map_limit)
    customer_addresses = customer_addresses_query.all()

    for addr in customer_addresses:
        subscriber_name = (
            f"{addr.first_name or ''} {addr.last_name or ''}".strip()
            or "Unknown Customer"
        )
        is_online = bool(addr.is_online)
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [addr.longitude, addr.latitude],
                },
                "properties": {
                    "id": str(addr.id),
                    "type": "customer",
                    "name": subscriber_name,
                    "address": addr.address_line1,
                    "city": addr.city or "",
                    "subscriber_id": str(addr.subscriber_id),
                    "is_online": is_online,
                },
            }
        )

    map_data = {"type": "FeatureCollection", "features": features}

    stats = {
        "pop_sites": db.query(func.count(PopSite.id))
        .filter(PopSite.is_active.is_(True))
        .scalar()
        or 0,
        "fdh_cabinets": db.query(func.count(FdhCabinet.id))
        .filter(FdhCabinet.is_active.is_(True))
        .scalar()
        or 0,
        "splice_closures": db.query(func.count(FiberSpliceClosure.id))
        .filter(FiberSpliceClosure.is_active.is_(True))
        .scalar()
        or 0,
        "access_points": db.query(func.count(FiberAccessPoint.id))
        .filter(FiberAccessPoint.is_active.is_(True))
        .scalar()
        or 0,
        "fiber_segments": len(segments),
        "customers": customer_total,
        "customers_online": online_count,
        "customers_offline": offline_count,
        "survey_points": 0,
    }

    return {
        "map_data": map_data,
        "stats": stats,
        "customer_count": customer_total,
        "customer_map_count": len(customer_addresses),
    }
