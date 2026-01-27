"""Admin network management web routes."""

import hashlib
import heapq
import ipaddress
import json
import math
import subprocess
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.schemas.catalog import (
    RadiusAttributeCreate,
    RadiusProfileCreate,
    RadiusProfileUpdate,
)
from app.schemas.network import (
    FiberSpliceCreate,
    FiberSpliceUpdate,
    FiberStrandCreate,
    FiberStrandUpdate,
    IpBlockCreate,
    IpPoolCreate,
    IpPoolUpdate,
)
from app.schemas.radius import (
    RadiusClientCreate,
    RadiusClientUpdate,
    RadiusServerCreate,
    RadiusServerUpdate,
)
from app.models.subscriber import Subscriber
from app.models.audit import AuditEvent
from app.services import audit as audit_service
from app.services import catalog as catalog_service
from app.services import fiber_change_requests as change_request_service
from app.services.audit_helpers import (
    build_changes_metadata,
    diff_dicts,
    extract_changes,
    format_changes,
    log_audit_event,
    model_to_dict,
)
from app.services import network as network_service
from app.services import radius as radius_service
from app.services import vendor as vendor_service

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])

RADIUS_CLIENT_EXCLUDE_FIELDS = {"shared_secret_hash"}

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _base_context(request: Request, db: Session, active_page: str, active_menu: str = "network"):
    from app.web.admin import get_sidebar_stats, get_current_user

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


def _build_audit_activities(
    db: Session,
    entity_type: str,
    entity_id: str,
    limit: int = 10,
) -> list[dict]:
    events = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type=entity_type,
        entity_id=entity_id,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=limit,
        offset=0,
    )
    actor_ids = {str(event.actor_id) for event in events if getattr(event, "actor_id", None)}
    people = {}
    if actor_ids:
        people = {
            str(person.id): person
            for person in db.query(Subscriber).filter(Subscriber.id.in_(actor_ids)).all()
        }
    activities = []
    for event in events:
        actor = people.get(str(event.actor_id)) if getattr(event, "actor_id", None) else None
        actor_name = f"{actor.first_name} {actor.last_name}".strip() if actor else "System"
        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes, max_items=2)
        activities.append(
            {
                "title": (event.action or "Activity").replace("_", " ").title(),
                "description": f"{actor_name}" + (f" · {change_summary}" if change_summary else ""),
                "occurred_at": event.occurred_at,
            }
        )
    return activities


def _build_audit_activities_for_types(
    db: Session,
    entity_types: list[str],
    limit: int = 10,
) -> list[dict]:
    if not entity_types:
        return []
    events = (
        db.query(AuditEvent)
        .filter(AuditEvent.entity_type.in_(entity_types))
        .filter(AuditEvent.is_active.is_(True))
        .order_by(AuditEvent.occurred_at.desc())
        .limit(limit)
        .all()
    )
    actor_ids = {str(event.actor_id) for event in events if getattr(event, "actor_id", None)}
    people = {}
    if actor_ids:
        people = {
            str(person.id): person
            for person in db.query(Subscriber).filter(Subscriber.id.in_(actor_ids)).all()
        }
    activities = []
    for event in events:
        actor = people.get(str(event.actor_id)) if getattr(event, "actor_id", None) else None
        actor_name = f"{actor.first_name} {actor.last_name}".strip() if actor else "System"
        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes, max_items=2)
        entity_label = (event.entity_type or "Activity").replace("_", " ").title()
        action_label = (event.action or "Activity").replace("_", " ").title()
        activities.append(
            {
                "title": f"{entity_label} {action_label}",
                "description": f"{actor_name}" + (f" · {change_summary}" if change_summary else ""),
                "occurred_at": event.occurred_at,
            }
        )
    return activities


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def _node_key(lon: float, lat: float) -> tuple[float, float]:
    return (round(lon, 6), round(lat, 6))


def _to_meters(lat0: float, lon: float, lat: float) -> tuple[float, float]:
    r = 6371000.0
    x = math.radians(lon) * r * math.cos(math.radians(lat0))
    y = math.radians(lat) * r
    return x, y


def _has_conflict(db: Session, change_request) -> bool:
    from app.services import fiber_change_requests as change_requests

    if not change_request.asset_id:
        return False
    _, model = change_requests._get_model(change_request.asset_type)
    asset = db.get(model, change_request.asset_id)
    if not asset or not getattr(asset, "updated_at", None):
        return False
    return asset.updated_at > change_request.created_at


def _serialize_asset(asset) -> dict:
    from sqlalchemy.inspection import inspect

    if not asset:
        return {}
    data: dict[str, object] = {}
    for column in inspect(asset).mapper.column_attrs:
        key = column.key
        if key in {"route_geom", "geom"}:
            continue
        value = getattr(asset, key)
        if hasattr(value, "value"):
            value = value.value
        data[key] = value
    return data


def _default_mast_context() -> dict[str, object]:
    return {"status": "active", "is_active": True, "metadata": ""}


def _format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "--"
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _format_bps(value: float | int | None) -> str:
    if value is None:
        return "--"
    units = ["bps", "Kbps", "Mbps", "Gbps", "Tbps"]
    size = float(value)
    unit_index = 0
    while size >= 1000 and unit_index < len(units) - 1:
        size /= 1000.0
        unit_index += 1
    return f"{size:.1f} {units[unit_index]}"


def _is_ipv6_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).version == 6
    except ValueError:
        return False


def _build_ping_command(host: str) -> list[str]:
    command = ["ping", "-c", "1", "-W", "2", host]
    if _is_ipv6_host(host):
        command.insert(1, "-6")
    return command


def _parse_mast_form(
    form, fallback_lat: float | None, fallback_lon: float | None
) -> tuple[bool, dict | None, str | None, dict[str, object]]:
    enabled = form.get("add_mast") == "true"
    mast_defaults: dict[str, object] = {
        "name": form.get("mast_name", "").strip(),
        "latitude": form.get("mast_latitude", "").strip(),
        "longitude": form.get("mast_longitude", "").strip(),
        "height_m": form.get("mast_height_m", "").strip(),
        "structure_type": form.get("mast_structure_type", "").strip(),
        "owner": form.get("mast_owner", "").strip(),
        "status": form.get("mast_status", "").strip() or "active",
        "notes": form.get("mast_notes", "").strip(),
        "metadata": form.get("mast_metadata", "").strip(),
        "is_active": form.get("mast_is_active") == "true",
    }

    if not enabled:
        mast_defaults = {**_default_mast_context(), **mast_defaults}
        mast_defaults["is_active"] = True
        return False, None, None, mast_defaults

    if not mast_defaults["name"]:
        return True, None, "Mast name is required.", mast_defaults

    def parse_float(value: str, label: str) -> tuple[float | None, str | None]:
        if not value:
            return None, None
        try:
            return float(value), None
        except ValueError:
            return None, f"{label} must be a valid number."

    lat, error = parse_float(mast_defaults["latitude"], "Mast latitude")
    if error:
        return True, None, error, mast_defaults
    lon, error = parse_float(mast_defaults["longitude"], "Mast longitude")
    if error:
        return True, None, error, mast_defaults

    if lat is None:
        lat = fallback_lat
    if lon is None:
        lon = fallback_lon
    if lat is None or lon is None:
        return True, None, "Mast latitude and longitude are required (or set POP site coordinates).", mast_defaults

    height_m, error = parse_float(mast_defaults["height_m"], "Mast height")
    if error:
        return True, None, error, mast_defaults

    metadata_raw = mast_defaults["metadata"]
    metadata = None
    if metadata_raw:
        try:
            metadata = json.loads(metadata_raw)
        except json.JSONDecodeError:
            return True, None, "Mast metadata must be valid JSON.", mast_defaults

    mast_data = {
        "name": mast_defaults["name"],
        "latitude": lat,
        "longitude": lon,
        "height_m": height_m,
        "structure_type": mast_defaults["structure_type"] or None,
        "owner": mast_defaults["owner"] or None,
        "status": mast_defaults["status"] or "active",
        "notes": mast_defaults["notes"] or None,
        "metadata_": metadata,
        "is_active": mast_defaults["is_active"],
    }

    mast_defaults = {**_default_mast_context(), **mast_defaults}
    return True, mast_data, None, mast_defaults


def _closest_point_on_segment(
    lat0: float,
    lon: float,
    lat: float,
    lon1: float,
    lat1: float,
    lon2: float,
    lat2: float,
) -> tuple[float, float, float]:
    px, py = _to_meters(lat0, lon, lat)
    ax, ay = _to_meters(lat0, lon1, lat1)
    bx, by = _to_meters(lat0, lon2, lat2)
    dx = bx - ax
    dy = by - ay
    if dx == 0 and dy == 0:
        return lon1, lat1, math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx = ax + t * dx
    cy = ay + t * dy
    c_lon = math.degrees(cx / (6371000.0 * math.cos(math.radians(lat0))))
    c_lat = math.degrees(cy / 6371000.0)
    return c_lon, c_lat, math.hypot(px - cx, py - cy)


def _build_fiber_graph(db: Session):
    from app.models.network import FiberSegment
    from sqlalchemy import func

    graph: dict[tuple[float, float], list[tuple[tuple[float, float], float]]] = {}
    edges: list[tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]]] = []

    def add_edge(a: tuple[float, float], b: tuple[float, float]) -> None:
        dist = _haversine_distance(a[1], a[0], b[1], b[0])
        graph.setdefault(a, []).append((b, dist))
        graph.setdefault(b, []).append((a, dist))

    segments = db.query(func.ST_AsGeoJSON(FiberSegment.route_geom)).filter(
        FiberSegment.is_active.is_(True),
        FiberSegment.route_geom.isnot(None),
    ).all()
    for (geojson_str,) in segments:
        if not geojson_str:
            continue
        geom = json.loads(geojson_str)
        if geom.get("type") == "LineString":
            lines = [geom["coordinates"]]
        elif geom.get("type") == "MultiLineString":
            lines = geom["coordinates"]
        else:
            continue
        for line in lines:
            if len(line) < 2:
                continue
            for i in range(len(line) - 1):
                lon1, lat1 = line[i]
                lon2, lat2 = line[i + 1]
                a = _node_key(lon1, lat1)
                b = _node_key(lon2, lat2)
                add_edge(a, b)
                edges.append((a, b, (lon1, lat1), (lon2, lat2)))
    return graph, edges


def _snap_to_graph(
    lat_in: float,
    lon_in: float,
    graph: dict[tuple[float, float], list[tuple[tuple[float, float], float]]],
    edges: list[tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]]],
    snap_max_m: float,
) -> tuple[tuple[float, float] | None, float]:
    if not edges:
        return None, float("inf")
    best = None
    best_dist = float("inf")
    best_edge = None
    for a, b, a_coord, b_coord in edges:
        c_lon, c_lat, dist = _closest_point_on_segment(
            lat_in, lon_in, lat_in, a_coord[0], a_coord[1], b_coord[0], b_coord[1]
        )
        if dist < best_dist:
            best_dist = dist
            best = _node_key(c_lon, c_lat)
            best_edge = (a, b)
    if best is None or best_dist > snap_max_m:
        return None, best_dist
    if best in graph:
        return best, best_dist
    if best_edge:
        a, b = best_edge
        # Connect the snapped point into the network.
        dist_a = _haversine_distance(best[1], best[0], a[1], a[0])
        dist_b = _haversine_distance(best[1], best[0], b[1], b[0])
        graph.setdefault(best, [])
        graph.setdefault(a, []).append((best, dist_a))
        graph.setdefault(b, []).append((best, dist_b))
        graph[best].append((a, dist_a))
        graph[best].append((b, dist_b))
    return best, best_dist


def _shortest_path(
    graph: dict[tuple[float, float], list[tuple[tuple[float, float], float]]],
    start: tuple[float, float],
    target: tuple[float, float],
) -> tuple[float | None, list[tuple[float, float]] | None]:
    dist_map = {start: 0.0}
    prev: dict[tuple[float, float], tuple[float, float] | None] = {start: None}
    heap = [(0.0, start)]
    visited = set()
    while heap:
        dist, node = heapq.heappop(heap)
        if node in visited:
            continue
        visited.add(node)
        if node == target:
            path = []
            cur = node
            while cur is not None:
                path.append(cur)
                cur = prev.get(cur)
            path.reverse()
            return dist, path
        for neighbor, weight in graph.get(node, []):
            if neighbor in visited:
                continue
            nd = dist + weight
            if nd < dist_map.get(neighbor, float("inf")):
                dist_map[neighbor] = nd
                prev[neighbor] = node
                heapq.heappush(heap, (nd, neighbor))
    return None, None


def _nearby_cabinets(db: Session, lat: float, lng: float, max_km: float):
    from app.models.network import FdhCabinet

    max_deg = max_km / 111.0
    if max_deg <= 0:
        return []
    return db.query(FdhCabinet).filter(
        FdhCabinet.is_active.is_(True),
        FdhCabinet.latitude.isnot(None),
        FdhCabinet.longitude.isnot(None),
        FdhCabinet.latitude.between(lat - max_deg, lat + max_deg),
        FdhCabinet.longitude.between(lng - max_deg, lng + max_deg),
    ).all()


@router.get("/olts", response_class=HTMLResponse)
def olts_list(request: Request, db: Session = Depends(get_db)):
    """List all OLT devices."""
    olts = network_service.olt_devices.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )

    olt_stats = {}
    for olt in olts:
        pon_ports = network_service.pon_ports.list(
            db=db,
            olt_id=str(olt.id),
            is_active=True,
            order_by="created_at",
            order_dir="asc",
            limit=1000,
            offset=0,
        )
        olt_stats[str(olt.id)] = {"pon_ports": len(pon_ports)}

    stats = {"total": len(olts), "active": sum(1 for o in olts if o.is_active)}

    context = _base_context(request, db, active_page="olts")
    context.update({"olts": olts, "olt_stats": olt_stats, "stats": stats})
    return templates.TemplateResponse("admin/network/olts/index.html", context)


@router.get("/olts/new", response_class=HTMLResponse)
def olt_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="olts")
    context.update({
        "olt": None,
        "action_url": "/admin/network/olts",
    })
    return templates.TemplateResponse("admin/network/olts/form.html", context)


def _olt_device_integrity_error_message(exc: Exception) -> str:
    message = str(exc)
    if "uq_olt_devices_hostname" in message:
        return "Hostname already exists"
    if "uq_olt_devices_mgmt_ip" in message:
        return "Management IP already exists"
    return "OLT device could not be saved due to a data conflict"


@router.post("/olts", response_class=HTMLResponse)
async def olt_create(request: Request, db: Session = Depends(get_db)):
    from types import SimpleNamespace
    from sqlalchemy.exc import IntegrityError
    from app.models.network import OLTDevice
    from app.schemas.network import OLTDeviceCreate

    form = await request.form()
    name = form.get("name", "").strip()
    hostname = form.get("hostname", "").strip() or None
    mgmt_ip = form.get("mgmt_ip", "").strip() or None

    if not name:
        context = _base_context(request, db, active_page="olts")
        context.update({
            "olt": None,
            "action_url": "/admin/network/olts",
            "error": "Name is required",
        })
        return templates.TemplateResponse("admin/network/olts/form.html", context)

    if hostname:
        existing = db.query(OLTDevice).filter(OLTDevice.hostname == hostname).first()
        if existing:
            context = _base_context(request, db, active_page="olts")
            context.update({
                "olt": None,
                "action_url": "/admin/network/olts",
                "error": "Hostname already exists",
            })
            return templates.TemplateResponse("admin/network/olts/form.html", context)

    if mgmt_ip:
        existing = db.query(OLTDevice).filter(OLTDevice.mgmt_ip == mgmt_ip).first()
        if existing:
            context = _base_context(request, db, active_page="olts")
            context.update({
                "olt": None,
                "action_url": "/admin/network/olts",
                "error": "Management IP already exists",
            })
            return templates.TemplateResponse("admin/network/olts/form.html", context)

    payload = OLTDeviceCreate(
        name=name,
        hostname=hostname,
        mgmt_ip=mgmt_ip,
        vendor=form.get("vendor", "").strip() or None,
        model=form.get("model", "").strip() or None,
        serial_number=form.get("serial_number", "").strip() or None,
        notes=form.get("notes", "").strip() or None,
        is_active=form.get("is_active") == "true",
    )

    try:
        olt = network_service.olt_devices.create(db=db, payload=payload)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="olt",
            entity_id=str(olt.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"name": olt.name, "mgmt_ip": olt.mgmt_ip or None},
        )
    except IntegrityError as exc:
        db.rollback()
        error = _olt_device_integrity_error_message(exc)
        olt_snapshot = SimpleNamespace(**payload.model_dump())
        context = _base_context(request, db, active_page="olts")
        context.update({
            "olt": olt_snapshot,
            "action_url": "/admin/network/olts",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/olts/form.html", context)

    return RedirectResponse(f"/admin/network/olts/{olt.id}", status_code=303)


@router.get("/olts/{olt_id}/edit", response_class=HTMLResponse)
def olt_edit(request: Request, olt_id: str, db: Session = Depends(get_db)):
    try:
        olt = network_service.olt_devices.get(db=db, device_id=olt_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="olts")
    context.update({
        "olt": olt,
        "action_url": f"/admin/network/olts/{olt.id}",
    })
    return templates.TemplateResponse("admin/network/olts/form.html", context)


@router.post("/olts/{olt_id}", response_class=HTMLResponse)
async def olt_update(request: Request, olt_id: str, db: Session = Depends(get_db)):
    from types import SimpleNamespace
    from sqlalchemy.exc import IntegrityError
    from app.models.network import OLTDevice
    from app.schemas.network import OLTDeviceUpdate

    try:
        olt = network_service.olt_devices.get(db=db, device_id=olt_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )

    form = await request.form()
    name = form.get("name", "").strip()
    hostname = form.get("hostname", "").strip() or None
    mgmt_ip = form.get("mgmt_ip", "").strip() or None
    if not name:
        context = _base_context(request, db, active_page="olts")
        context.update({
            "olt": olt,
            "action_url": f"/admin/network/olts/{olt.id}",
            "error": "Name is required",
        })
        return templates.TemplateResponse("admin/network/olts/form.html", context)

    if hostname:
        existing = (
            db.query(OLTDevice)
            .filter(OLTDevice.hostname == hostname)
            .filter(OLTDevice.id != olt.id)
            .first()
        )
        if existing:
            context = _base_context(request, db, active_page="olts")
            context.update({
                "olt": olt,
                "action_url": f"/admin/network/olts/{olt.id}",
                "error": "Hostname already exists",
            })
            return templates.TemplateResponse("admin/network/olts/form.html", context)

    if mgmt_ip:
        existing = (
            db.query(OLTDevice)
            .filter(OLTDevice.mgmt_ip == mgmt_ip)
            .filter(OLTDevice.id != olt.id)
            .first()
        )
        if existing:
            context = _base_context(request, db, active_page="olts")
            context.update({
                "olt": olt,
                "action_url": f"/admin/network/olts/{olt.id}",
                "error": "Management IP already exists",
            })
            return templates.TemplateResponse("admin/network/olts/form.html", context)

    payload = OLTDeviceUpdate(
        name=name,
        hostname=hostname,
        mgmt_ip=mgmt_ip,
        vendor=form.get("vendor", "").strip() or None,
        model=form.get("model", "").strip() or None,
        serial_number=form.get("serial_number", "").strip() or None,
        notes=form.get("notes", "").strip() or None,
        is_active=form.get("is_active") == "true",
    )

    try:
        before_snapshot = model_to_dict(olt)
        olt = network_service.olt_devices.update(db=db, device_id=olt_id, payload=payload)
        after = network_service.olt_devices.get(db=db, device_id=olt_id)
        after_snapshot = model_to_dict(after)
        changes = diff_dicts(before_snapshot, after_snapshot)
        metadata_payload = {"changes": changes} if changes else None
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="olt",
            entity_id=str(olt_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata_payload,
        )
    except IntegrityError as exc:
        db.rollback()
        error = _olt_device_integrity_error_message(exc)
        olt_snapshot = SimpleNamespace(**payload.model_dump())
        context = _base_context(request, db, active_page="olts")
        context.update({
            "olt": olt_snapshot,
            "action_url": f"/admin/network/olts/{olt_id}",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/olts/form.html", context)

    return RedirectResponse(f"/admin/network/olts/{olt.id}", status_code=303)


@router.get("/olts/{olt_id}", response_class=HTMLResponse)
def olt_detail(request: Request, olt_id: str, db: Session = Depends(get_db)):
    try:
        olt = network_service.olt_devices.get(db=db, device_id=olt_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "OLT not found"},
            status_code=404,
        )

    # Get PON ports
    pon_ports = network_service.pon_ports.list(
        db=db,
        olt_id=olt_id,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )

    # Get ONT assignments for this OLT
    ont_assignments = []
    for port in pon_ports:
        port_assignments = network_service.ont_assignments.list(
            db=db,
            pon_port_id=str(port.id),
            active=True,
            order_by="created_at",
            order_dir="desc",
            limit=100,
            offset=0,
        )
        ont_assignments.extend(port_assignments)

    activities = _build_audit_activities(db, "olt", str(olt_id))
    context = _base_context(request, db, active_page="olts")
    context.update({
        "olt": olt,
        "pon_ports": pon_ports,
        "ont_assignments": ont_assignments,
        "activities": activities,
    })
    return templates.TemplateResponse("admin/network/olts/detail.html", context)


@router.get("/onts", response_class=HTMLResponse)
def onts_list(request: Request, status: str | None = None, db: Session = Depends(get_db)):
    """List all ONT/CPE devices."""
    active_onts = network_service.ont_units.list(
        db=db,
        is_active=True,
        order_by="serial_number",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    inactive_onts = network_service.ont_units.list(
        db=db,
        is_active=False,
        order_by="serial_number",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    all_onts = active_onts + inactive_onts

    status_filter = (status or "all").strip().lower()
    if status_filter == "active":
        onts = active_onts
    elif status_filter == "inactive":
        onts = inactive_onts
    else:
        onts = all_onts

    cpes = network_service.cpe_devices.list(
        db=db,
        account_id=None,
        subscription_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=100,
        offset=0,
    )

    stats = {
        "total_onts": len(all_onts),
        "active_onts": len(active_onts),
        "inactive_onts": len(inactive_onts),
        "total_cpes": len(cpes),
        "total": len(all_onts) + len(cpes),
    }

    context = _base_context(request, db, active_page="onts")
    context.update({
        "onts": onts,
        "cpes": cpes,
        "stats": stats,
        "status_filter": status_filter,
    })
    return templates.TemplateResponse("admin/network/onts/index.html", context)


@router.get("/onts/new", response_class=HTMLResponse)
def ont_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="onts")
    context.update({
        "ont": None,
        "action_url": "/admin/network/onts",
    })
    return templates.TemplateResponse("admin/network/onts/form.html", context)


def _ont_unit_integrity_error_message(exc: Exception) -> str:
    message = str(exc)
    if "uq_ont_units_serial_number" in message:
        return "Serial number already exists"
    return "ONT could not be saved due to a data conflict"


@router.post("/onts", response_class=HTMLResponse)
async def ont_create(request: Request, db: Session = Depends(get_db)):
    from types import SimpleNamespace
    from sqlalchemy.exc import IntegrityError
    from app.schemas.network import OntUnitCreate

    form = await request.form()
    serial_number = form.get("serial_number", "").strip()

    if not serial_number:
        context = _base_context(request, db, active_page="onts")
        context.update({
            "ont": None,
            "action_url": "/admin/network/onts",
            "error": "Serial number is required",
        })
        return templates.TemplateResponse("admin/network/onts/form.html", context)

    payload = OntUnitCreate(
        serial_number=serial_number,
        vendor=form.get("vendor", "").strip() or None,
        model=form.get("model", "").strip() or None,
        firmware_version=form.get("firmware_version", "").strip() or None,
        notes=form.get("notes", "").strip() or None,
        is_active=form.get("is_active") == "true",
    )

    if payload.is_active:
        context = _base_context(request, db, active_page="onts")
        context.update({
            "ont": payload,
            "action_url": "/admin/network/onts",
            "error": "New ONTs must be inactive until assigned to a customer.",
        })
        return templates.TemplateResponse("admin/network/onts/form.html", context)

    try:
        ont = network_service.ont_units.create(db=db, payload=payload)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="ont",
            entity_id=str(ont.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"serial_number": ont.serial_number},
        )
    except IntegrityError as exc:
        db.rollback()
        error = _ont_unit_integrity_error_message(exc)
        ont_snapshot = SimpleNamespace(**payload.model_dump())
        context = _base_context(request, db, active_page="onts")
        context.update({
            "ont": ont_snapshot,
            "action_url": "/admin/network/onts",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/onts/form.html", context)

    return RedirectResponse(f"/admin/network/onts/{ont.id}", status_code=303)


@router.get("/onts/{ont_id}/edit", response_class=HTMLResponse)
def ont_edit(request: Request, ont_id: str, db: Session = Depends(get_db)):
    try:
        ont = network_service.ont_units.get(db=db, unit_id=ont_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="onts")
    context.update({
        "ont": ont,
        "action_url": f"/admin/network/onts/{ont.id}",
    })
    return templates.TemplateResponse("admin/network/onts/form.html", context)


@router.get("/onts/{ont_id}", response_class=HTMLResponse)
def ont_detail(request: Request, ont_id: str, db: Session = Depends(get_db)):
    try:
        ont = network_service.ont_units.get(db=db, unit_id=ont_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    # Get active assignment
    assignments = network_service.ont_assignments.list(
        db=db,
        ont_unit_id=ont_id,
        pon_port_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assignment = next((a for a in assignments if a.active), None)

    activities = _build_audit_activities(db, "ont", str(ont_id))
    context = _base_context(request, db, active_page="onts")
    context.update({
        "ont": ont,
        "assignment": assignment,
        "activities": activities,
    })
    return templates.TemplateResponse("admin/network/onts/detail.html", context)


@router.get("/onts/{ont_id}/assign", response_class=HTMLResponse)
def ont_assign_new(request: Request, ont_id: str, db: Session = Depends(get_db)):
    from app.services import catalog as catalog_service
    from app.services import subscriber as subscriber_service

    try:
        ont = network_service.ont_units.get(db=db, unit_id=ont_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    pon_ports = network_service.pon_ports.list(
        db=db,
        olt_id=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )

    accounts = subscriber_service.accounts.list(
        db=db,
        subscriber_id=None,
        reseller_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )

    subscriptions = catalog_service.subscriptions.list(
        db=db,
        account_id=None,
        offer_id=None,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )

    addresses = subscriber_service.addresses.list(
        db=db,
        subscriber_id=None,
        account_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )

    context = _base_context(request, db, active_page="onts")
    context.update({
        "ont": ont,
        "pon_ports": pon_ports,
        "accounts": accounts,
        "subscriptions": subscriptions,
        "addresses": addresses,
        "action_url": f"/admin/network/onts/{ont.id}/assign",
    })
    return templates.TemplateResponse("admin/network/onts/assign.html", context)


@router.post("/onts/{ont_id}/assign", response_class=HTMLResponse)
async def ont_assign_create(request: Request, ont_id: str, db: Session = Depends(get_db)):
    from datetime import datetime, timezone
    from app.schemas.network import OntAssignmentCreate, OntUnitUpdate
    from app.services import catalog as catalog_service
    from app.services import subscriber as subscriber_service

    try:
        ont = network_service.ont_units.get(db=db, unit_id=ont_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    form = await request.form()
    pon_port_id = form.get("pon_port_id", "").strip()
    account_id = form.get("account_id", "").strip() or None
    subscription_id = form.get("subscription_id", "").strip() or None
    service_address_id = form.get("service_address_id", "").strip() or None
    notes = form.get("notes", "").strip() or None

    if not pon_port_id:
        error = "PON port is required"
    elif not account_id:
        error = "Subscriber account is required"
    else:
        error = None

    assignments = network_service.ont_assignments.list(
        db=db,
        ont_unit_id=ont_id,
        pon_port_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=20,
        offset=0,
    )
    if any(a.active for a in assignments):
        error = "This ONT is already assigned"

    if error:
        pon_ports = network_service.pon_ports.list(
            db=db,
            olt_id=None,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=500,
            offset=0,
        )
        accounts = subscriber_service.accounts.list(
            db=db,
            subscriber_id=None,
            reseller_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=500,
            offset=0,
        )
        subscriptions = catalog_service.subscriptions.list(
            db=db,
            account_id=None,
            offer_id=None,
            status=None,
            order_by="created_at",
            order_dir="desc",
            limit=500,
            offset=0,
        )
        addresses = subscriber_service.addresses.list(
            db=db,
            subscriber_id=None,
            account_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=500,
            offset=0,
        )
        context = _base_context(request, db, active_page="onts")
        context.update({
            "ont": ont,
            "pon_ports": pon_ports,
            "accounts": accounts,
            "subscriptions": subscriptions,
            "addresses": addresses,
            "action_url": f"/admin/network/onts/{ont.id}/assign",
            "error": error,
            "form": {
                "pon_port_id": pon_port_id,
                "account_id": account_id,
                "subscription_id": subscription_id,
                "service_address_id": service_address_id,
                "notes": notes,
            },
        })
        return templates.TemplateResponse("admin/network/onts/assign.html", context)

    payload = OntAssignmentCreate(
        ont_unit_id=ont.id,
        pon_port_id=pon_port_id,
        account_id=account_id,
        subscription_id=subscription_id,
        service_address_id=service_address_id,
        assigned_at=datetime.now(timezone.utc),
        active=True,
        notes=notes,
    )

    network_service.ont_assignments.create(db=db, payload=payload)
    network_service.ont_units.update(
        db=db,
        unit_id=str(ont.id),
        payload=OntUnitUpdate(is_active=True),
    )

    return RedirectResponse(f"/admin/network/onts/{ont.id}", status_code=303)


@router.post("/onts/{ont_id}", response_class=HTMLResponse)
async def ont_update(request: Request, ont_id: str, db: Session = Depends(get_db)):
    from types import SimpleNamespace
    from sqlalchemy.exc import IntegrityError
    from app.schemas.network import OntUnitUpdate

    try:
        ont = network_service.ont_units.get(db=db, unit_id=ont_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "ONT not found"},
            status_code=404,
        )

    form = await request.form()
    serial_number = form.get("serial_number", "").strip()

    if not serial_number:
        context = _base_context(request, db, active_page="onts")
        context.update({
            "ont": ont,
            "action_url": f"/admin/network/onts/{ont.id}",
            "error": "Serial number is required",
        })
        return templates.TemplateResponse("admin/network/onts/form.html", context)

    payload = OntUnitUpdate(
        serial_number=serial_number,
        vendor=form.get("vendor", "").strip() or None,
        model=form.get("model", "").strip() or None,
        firmware_version=form.get("firmware_version", "").strip() or None,
        notes=form.get("notes", "").strip() or None,
        is_active=form.get("is_active") == "true",
    )

    try:
        before_snapshot = model_to_dict(ont)
        ont = network_service.ont_units.update(db=db, unit_id=ont_id, payload=payload)
        after = network_service.ont_units.get(db=db, unit_id=ont_id)
        after_snapshot = model_to_dict(after)
        changes = diff_dicts(before_snapshot, after_snapshot)
        metadata_payload = {"changes": changes} if changes else None
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="ont",
            entity_id=str(ont_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata_payload,
        )
    except IntegrityError as exc:
        db.rollback()
        error = _ont_unit_integrity_error_message(exc)
        ont_snapshot = SimpleNamespace(**payload.model_dump())
        context = _base_context(request, db, active_page="onts")
        context.update({
            "ont": ont_snapshot,
            "action_url": f"/admin/network/onts/{ont_id}",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/onts/form.html", context)

    return RedirectResponse(f"/admin/network/onts/{ont.id}", status_code=303)


@router.get("/cpes/{cpe_id}", response_class=HTMLResponse)
def cpe_detail(request: Request, cpe_id: str, db: Session = Depends(get_db)):
    try:
        cpe = network_service.cpe_devices.get(db=db, cpe_device_id=cpe_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "CPE not found"},
            status_code=404,
        )

    # Get ports for this CPE
    from app.models.network import Port
    ports = db.query(Port).filter(Port.device_id == cpe.id).all()

    activities = _build_audit_activities(db, "cpe", str(cpe_id))
    context = _base_context(request, db, active_page="onts")
    context.update({
        "cpe": cpe,
        "ports": ports,
        "activities": activities,
    })
    return templates.TemplateResponse("admin/network/cpes/detail.html", context)


def _collect_devices(db: Session) -> list[dict]:
    devices: list[dict] = []

    olts = network_service.olt_devices.list(
        db=db, is_active=True, order_by="name", order_dir="asc", limit=500, offset=0
    )
    for olt in olts:
        devices.append({
            "id": str(olt.id),
            "name": olt.name,
            "type": "olt",
            "serial_number": getattr(olt, "serial_number", None),
            "ip_address": getattr(olt, "management_ip", None) or getattr(olt, "mgmt_ip", None),
            "vendor": olt.vendor,
            "model": olt.model,
            "status": "online" if olt.is_active else "offline",
            "last_seen": getattr(olt, "last_seen", None),
            "subscriber": None,
        })

    onts = network_service.ont_units.list(
        db=db, is_active=True, order_by="serial_number", order_dir="asc", limit=500, offset=0
    )
    for ont in onts:
        devices.append({
            "id": str(ont.id),
            "name": getattr(ont, "name", None) or ont.serial_number,
            "type": "ont",
            "serial_number": ont.serial_number,
            "ip_address": getattr(ont, "ip_address", None),
            "vendor": ont.vendor,
            "model": ont.model,
            "status": "online" if ont.is_active else "offline",
            "last_seen": getattr(ont, "last_seen", None),
            "subscriber": None,
        })

    cpes = network_service.cpe_devices.list(
        db=db, account_id=None, subscription_id=None, order_by="created_at", order_dir="desc", limit=500, offset=0
    )
    for cpe in cpes:
        devices.append({
            "id": str(cpe.id),
            "name": getattr(cpe, "name", None) or getattr(cpe, "serial_number", str(cpe.id)[:8]),
            "type": "cpe",
            "serial_number": getattr(cpe, "serial_number", None),
            "ip_address": getattr(cpe, "ip_address", None),
            "vendor": getattr(cpe, "vendor", None),
            "model": getattr(cpe, "model", None),
            "status": "online",
            "last_seen": getattr(cpe, "last_seen", None),
            "subscriber": None,
        })

    return devices


def _device_matches_search(device: dict, term: str) -> bool:
    haystack = [
        device.get("name"),
        device.get("serial_number"),
        device.get("ip_address"),
        device.get("vendor"),
        device.get("model"),
        device.get("type"),
    ]
    return any((value or "").lower().find(term) != -1 for value in haystack)


@router.get("/devices", response_class=HTMLResponse)
def devices_list(
    request: Request,
    device_type: str = None,
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
    db: Session = Depends(get_db),
):
    """List all network devices."""
    devices = _collect_devices(db)

    if device_type and device_type != "all":
        devices = [d for d in devices if d["type"] == device_type]

    term = (search or "").strip().lower()
    if term:
        devices = [device for device in devices if _device_matches_search(device, term)]

    status_filter = (status or "").strip().lower()
    if status_filter:
        devices = [d for d in devices if (d.get("status") or "").lower() == status_filter]

    vendor_filter = (vendor or "").strip().lower()
    if vendor_filter:
        devices = [d for d in devices if (d.get("vendor") or "").lower() == vendor_filter]

    stats = {
        "total": len(devices),
        "olt": sum(1 for d in devices if d["type"] == "olt"),
        "ont": sum(1 for d in devices if d["type"] == "ont"),
        "cpe": sum(1 for d in devices if d["type"] == "cpe"),
        "online": sum(1 for d in devices if d["status"] == "online"),
        "offline": sum(1 for d in devices if d["status"] == "offline"),
        "warning": 0,
        "unprovisioned": 0,
    }

    context = _base_context(request, db, active_page="devices")
    context.update(
        {
            "devices": devices,
            "stats": stats,
            "device_type": device_type,
            "search": search or "",
            "status": status or "",
            "vendor": vendor or "",
        }
    )
    return templates.TemplateResponse("admin/network/devices/index.html", context)


@router.get("/devices/search", response_class=HTMLResponse)
def devices_search(request: Request, search: str = "", db: Session = Depends(get_db)):
    devices = _collect_devices(db)
    term = search.strip().lower()
    if term:
        devices = [
            device for device in devices
            if _device_matches_search(device, term)
        ]
    return templates.TemplateResponse(
        "admin/network/devices/_table_rows.html",
        {"request": request, "devices": devices},
    )


@router.get("/devices/filter", response_class=HTMLResponse)
def devices_filter(
    request: Request,
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
    db: Session = Depends(get_db),
):
    devices = _collect_devices(db)
    term = (search or "").strip().lower()
    if term:
        devices = [device for device in devices if _device_matches_search(device, term)]
    status_filter = (status or "").strip().lower()
    vendor_filter = (vendor or "").strip().lower()

    if status_filter:
        devices = [d for d in devices if (d.get("status") or "").lower() == status_filter]
    if vendor_filter:
        devices = [d for d in devices if (d.get("vendor") or "").lower() == vendor_filter]

    return templates.TemplateResponse(
        "admin/network/devices/_table_rows.html",
        {"request": request, "devices": devices},
    )


@router.post("/devices/discover", response_class=HTMLResponse)
def devices_discover(request: Request, db: Session = Depends(get_db)):
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        "Discovery queued. Devices will appear as they are detected."
        "</div>"
    )


@router.get("/devices/create", response_class=HTMLResponse)
def device_create(request: Request, db: Session = Depends(get_db)):
    # Redirect to the more specific device creation pages
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin/network/core-devices/new", status_code=302)


@router.get("/devices/{device_id}", response_class=HTMLResponse)
def device_detail(request: Request, device_id: str, db: Session = Depends(get_db)):
    # Try to find the device in various device tables
    from app.models.network_monitoring import NetworkDevice

    device = db.query(NetworkDevice).filter(NetworkDevice.id == device_id).first()
    if device:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=f"/admin/network/core-devices/{device_id}", status_code=302)

    # Try OLT
    try:
        olt = network_service.olt_devices.get(db=db, device_id=device_id)
        if olt:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=f"/admin/network/olts/{device_id}", status_code=302)
    except Exception:
        pass

    # Try ONT
    try:
        ont = network_service.ont_units.get(db=db, unit_id=device_id)
        if ont:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=f"/admin/network/onts/{device_id}", status_code=302)
    except Exception:
        pass

    # Try CPE
    try:
        cpe = network_service.cpe_devices.get(db=db, cpe_device_id=device_id)
        if cpe:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=f"/admin/network/cpes/{device_id}", status_code=302)
    except Exception:
        pass

    return templates.TemplateResponse(
        "admin/errors/404.html",
        {"request": request, "message": "Device not found"},
        status_code=404,
    )


@router.post("/devices/{device_id}/ping", response_class=HTMLResponse)
def device_ping(request: Request, device_id: str, db: Session = Depends(get_db)):
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        f"Ping queued for device {device_id}."
        "</div>"
    )


@router.post("/devices/{device_id}/reboot", response_class=HTMLResponse)
def device_reboot(request: Request, device_id: str, db: Session = Depends(get_db)):
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        f"Reboot request queued for device {device_id}."
        "</div>"
    )


@router.get("/ip-management", response_class=HTMLResponse)
def ip_management(request: Request, db: Session = Depends(get_db)):
    """IP address management page - consolidated view with tabs."""
    pools = network_service.ip_pools.list(
        db=db, ip_version=None, is_active=None, order_by="name", order_dir="asc", limit=100, offset=0
    )

    blocks = network_service.ip_blocks.list(
        db=db, pool_id=None, is_active=None, order_by="cidr", order_dir="asc", limit=100, offset=0
    )

    assignments = network_service.ip_assignments.list(
        db=db, account_id=None, subscription_id=None, is_active=True, order_by="created_at", order_dir="desc", limit=100, offset=0
    )

    # Get IPv4 and IPv6 addresses for the Addresses tab
    ipv4_addresses = network_service.ipv4_addresses.list(
        db=db, pool_id=None, is_reserved=None, order_by="address", order_dir="asc", limit=100, offset=0
    )
    ipv6_addresses = network_service.ipv6_addresses.list(
        db=db, pool_id=None, is_reserved=None, order_by="address", order_dir="asc", limit=100, offset=0
    )

    stats = {
        "total_pools": len(pools),
        "total_blocks": len(blocks),
        "total_assignments": len(assignments),
        "total_addresses": len(ipv4_addresses) + len(ipv6_addresses),
    }

    context = _base_context(request, db, active_page="ip-management", active_menu="network")
    context.update({
        "pools": pools,
        "blocks": blocks,
        "assignments": assignments,
        "ipv4_addresses": ipv4_addresses,
        "ipv6_addresses": ipv6_addresses,
        "stats": stats,
        "activities": _build_audit_activities_for_types(
            db,
            ["ip_pool", "ip_block"],
            limit=5,
        ),
    })
    return templates.TemplateResponse("admin/network/ip-management/index.html", context)


@router.get("/ip-management/pools/new", response_class=HTMLResponse)
def ip_pool_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
    context.update({
        "pool": None,
        "action_url": "/admin/network/ip-management/pools",
    })
    return templates.TemplateResponse("admin/network/ip-management/pool_form.html", context)


@router.get("/ip-management/pools", response_class=HTMLResponse)
def ip_pools_redirect():
    return RedirectResponse("/admin/network/ip-management", status_code=303)


@router.get("/ip-management/blocks/new", response_class=HTMLResponse)
def ip_block_new(
    request: Request,
    pool_id: str | None = None,
    db: Session = Depends(get_db),
):
    pools = network_service.ip_pools.list(
        db=db,
        ip_version=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    block = {"pool_id": pool_id} if pool_id else None
    context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
    context.update({
        "block": block,
        "pools": pools,
        "action_url": "/admin/network/ip-management/blocks",
    })
    return templates.TemplateResponse("admin/network/ip-management/block_form.html", context)


@router.get("/ip-management/blocks", response_class=HTMLResponse)
def ip_blocks_redirect():
    return RedirectResponse("/admin/network/ip-management", status_code=303)


@router.post("/ip-management/blocks", response_class=HTMLResponse)
async def ip_block_create(request: Request, db: Session = Depends(get_db)):
    pools = network_service.ip_pools.list(
        db=db,
        ip_version=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )

    form = await request.form()
    pool_id = form.get("pool_id", "").strip()
    cidr = form.get("cidr", "").strip()
    notes = form.get("notes", "").strip()
    is_active = form.get("is_active") == "true"

    error = None
    if not pool_id:
        error = "IP pool is required."
    elif not cidr:
        error = "CIDR block is required."

    block_data = {
        "pool_id": pool_id,
        "cidr": cidr,
        "notes": notes or None,
        "is_active": is_active,
    }

    if error:
        context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
        context.update({
            "block": block_data,
            "pools": pools,
            "action_url": "/admin/network/ip-management/blocks",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/ip-management/block_form.html", context)

    try:
        payload = IpBlockCreate(
            pool_id=pool_id,
            cidr=cidr,
            notes=notes or None,
            is_active=is_active,
        )
        block = network_service.ip_blocks.create(db=db, payload=payload)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="ip_block",
            entity_id=str(block.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"cidr": block.cidr, "pool_id": str(block.pool_id)},
        )
        return RedirectResponse("/admin/network/ip-management", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
    context.update({
        "block": block_data,
        "pools": pools,
        "action_url": "/admin/network/ip-management/blocks",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/ip-management/block_form.html", context)


@router.post("/ip-management/pools", response_class=HTMLResponse)
async def ip_pool_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    name = form.get("name", "").strip()
    ip_version = form.get("ip_version", "").strip()
    cidr = form.get("cidr", "").strip()
    gateway = form.get("gateway", "").strip()
    dns_primary = form.get("dns_primary", "").strip()
    dns_secondary = form.get("dns_secondary", "").strip()
    notes = form.get("notes", "").strip()
    is_active = form.get("is_active") == "true"

    error = None
    if not name:
        error = "Pool name is required."
    elif not ip_version:
        error = "IP version is required."
    elif not cidr:
        error = "CIDR block is required."

    pool_data = {
        "name": name,
        "ip_version": {"value": ip_version},
        "cidr": cidr,
        "gateway": gateway or None,
        "dns_primary": dns_primary or None,
        "dns_secondary": dns_secondary or None,
        "notes": notes or None,
        "is_active": is_active,
    }

    if error:
        context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
        context.update({
            "pool": pool_data,
            "action_url": "/admin/network/ip-management/pools",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/ip-management/pool_form.html", context)

    try:
        payload = IpPoolCreate(
            name=name,
            ip_version=ip_version,
            cidr=cidr,
            gateway=gateway or None,
            dns_primary=dns_primary or None,
            dns_secondary=dns_secondary or None,
            notes=notes or None,
            is_active=is_active,
        )
        pool = network_service.ip_pools.create(db=db, payload=payload)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="ip_pool",
            entity_id=str(pool.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"name": pool.name, "cidr": pool.cidr},
        )
        return RedirectResponse("/admin/network/ip-management", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
    context.update({
        "pool": pool_data,
        "action_url": "/admin/network/ip-management/pools",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/ip-management/pool_form.html", context)


@router.get("/ip-management/pools/{pool_id}", response_class=HTMLResponse)
def ip_pool_detail(request: Request, pool_id: str, db: Session = Depends(get_db)):
    try:
        pool = network_service.ip_pools.get(db=db, pool_id=pool_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "IP Pool not found"},
            status_code=404,
        )

    # Get blocks for this pool
    blocks = network_service.ip_blocks.list(
        db=db,
        pool_id=pool_id,
        is_active=None,
        order_by="cidr",
        order_dir="asc",
        limit=100,
        offset=0,
    )

    # Get IP addresses for this pool
    from app.models.network import IPv4Address, IPv6Address
    if pool.ip_version.value == "ipv4":
        assignments = db.query(IPv4Address).filter(IPv4Address.pool_id == pool.id).limit(100).all()
    else:
        assignments = db.query(IPv6Address).filter(IPv6Address.pool_id == pool.id).limit(100).all()

    activities = _build_audit_activities(db, "ip_pool", str(pool_id))
    context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
    context.update({
        "pool": pool,
        "blocks": blocks,
        "assignments": assignments,
        "activities": activities,
    })
    return templates.TemplateResponse("admin/network/ip-management/pool_detail.html", context)


@router.get("/ip-management/pools/{pool_id}/edit", response_class=HTMLResponse)
def ip_pool_edit(request: Request, pool_id: str, db: Session = Depends(get_db)):
    try:
        pool = network_service.ip_pools.get(db=db, pool_id=pool_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "IP Pool not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
    context.update({
        "pool": pool,
        "action_url": f"/admin/network/ip-management/pools/{pool_id}",
    })
    return templates.TemplateResponse("admin/network/ip-management/pool_form.html", context)


@router.post("/ip-management/pools/{pool_id}", response_class=HTMLResponse)
async def ip_pool_update(request: Request, pool_id: str, db: Session = Depends(get_db)):
    try:
        pool = network_service.ip_pools.get(db=db, pool_id=pool_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "IP Pool not found"},
            status_code=404,
        )

    form = await request.form()
    name = form.get("name", "").strip()
    ip_version = form.get("ip_version", "").strip()
    cidr = form.get("cidr", "").strip()
    gateway = form.get("gateway", "").strip()
    dns_primary = form.get("dns_primary", "").strip()
    dns_secondary = form.get("dns_secondary", "").strip()
    notes = form.get("notes", "").strip()
    is_active = form.get("is_active") == "true"

    error = None
    if not name:
        error = "Pool name is required."
    elif not ip_version:
        error = "IP version is required."
    elif not cidr:
        error = "CIDR block is required."

    pool_data = {
        "id": pool.id,
        "name": name,
        "ip_version": {"value": ip_version},
        "cidr": cidr,
        "gateway": gateway or None,
        "dns_primary": dns_primary or None,
        "dns_secondary": dns_secondary or None,
        "notes": notes or None,
        "is_active": is_active,
    }

    if error:
        context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
        context.update({
            "pool": pool_data,
            "action_url": f"/admin/network/ip-management/pools/{pool_id}",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/ip-management/pool_form.html", context)

    try:
        before_snapshot = model_to_dict(pool)
        payload = IpPoolUpdate(
            name=name,
            ip_version=ip_version,
            cidr=cidr,
            gateway=gateway or None,
            dns_primary=dns_primary or None,
            dns_secondary=dns_secondary or None,
            notes=notes or None,
            is_active=is_active,
        )
        network_service.ip_pools.update(db=db, pool_id=pool_id, payload=payload)
        after = network_service.ip_pools.get(db=db, pool_id=pool_id)
        after_snapshot = model_to_dict(after)
        changes = diff_dicts(before_snapshot, after_snapshot)
        metadata_payload = {"changes": changes} if changes else None
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="ip_pool",
            entity_id=str(pool_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata_payload,
        )
        return RedirectResponse(f"/admin/network/ip-management/pools/{pool_id}", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="ip-management", active_menu="ip-address")
    context.update({
        "pool": pool_data,
        "action_url": f"/admin/network/ip-management/pools/{pool_id}",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/ip-management/pool_form.html", context)


@router.get("/ip-management/calculator", response_class=HTMLResponse)
def ip_calculator(request: Request, db: Session = Depends(get_db)):
    """IP subnet calculator tool."""
    context = _base_context(request, db, active_page="ip-calculator", active_menu="ip-address")
    return templates.TemplateResponse("admin/network/ip-management/calculator.html", context)


@router.get("/ip-management/assignments", response_class=HTMLResponse)
def ip_assignments_list(request: Request, db: Session = Depends(get_db)):
    """List all IP assignments."""
    assignments = network_service.ip_assignments.list(
        db=db, is_active=True, order_by="created_at", order_dir="desc", limit=100, offset=0
    )

    stats = {
        "total": len(assignments),
        "active": sum(1 for a in assignments if a.is_active),
    }

    context = _base_context(request, db, active_page="ip-assignments", active_menu="ip-address")
    context.update({
        "assignments": assignments,
        "stats": stats,
        "activities": _build_audit_activities_for_types(
            db,
            ["ip_pool", "ip_block"],
            limit=5,
        ),
    })
    return templates.TemplateResponse("admin/network/ip-management/assignments.html", context)


@router.get("/ip-management/ipv4", response_class=HTMLResponse)
def ipv4_addresses_list(request: Request, db: Session = Depends(get_db)):
    """List all IPv4 addresses."""
    addresses = network_service.ipv4_addresses.list(
        db=db, pool_id=None, is_reserved=None, order_by="address", order_dir="asc", limit=200, offset=0
    )
    pools = network_service.ip_pools.list(
        db=db, ip_version="ipv4", is_active=True, order_by="name", order_dir="asc", limit=100, offset=0
    )

    stats = {
        "total": len(addresses),
        "reserved": sum(1 for a in addresses if a.is_reserved),
        "available": sum(1 for a in addresses if not a.is_reserved),
    }

    context = _base_context(request, db, active_page="ipv4-addresses", active_menu="ip-address")
    context.update({
        "addresses": addresses,
        "pools": pools,
        "stats": stats,
        "ip_version": "ipv4",
        "activities": _build_audit_activities_for_types(
            db,
            ["ip_pool", "ip_block"],
            limit=5,
        ),
    })
    return templates.TemplateResponse("admin/network/ip-management/addresses.html", context)


@router.get("/ip-management/ipv6", response_class=HTMLResponse)
def ipv6_addresses_list(request: Request, db: Session = Depends(get_db)):
    """List all IPv6 addresses."""
    addresses = network_service.ipv6_addresses.list(
        db=db, pool_id=None, is_reserved=None, order_by="address", order_dir="asc", limit=200, offset=0
    )
    pools = network_service.ip_pools.list(
        db=db, ip_version="ipv6", is_active=True, order_by="name", order_dir="asc", limit=100, offset=0
    )

    stats = {
        "total": len(addresses),
        "reserved": sum(1 for a in addresses if a.is_reserved),
        "available": sum(1 for a in addresses if not a.is_reserved),
    }

    context = _base_context(request, db, active_page="ipv6-addresses", active_menu="ip-address")
    context.update({
        "addresses": addresses,
        "pools": pools,
        "stats": stats,
        "ip_version": "ipv6",
        "activities": _build_audit_activities_for_types(
            db,
            ["ip_pool", "ip_block"],
            limit=5,
        ),
    })
    return templates.TemplateResponse("admin/network/ip-management/addresses.html", context)


@router.get("/ip-management/pools", response_class=HTMLResponse)
def ip_pools_list(request: Request, db: Session = Depends(get_db)):
    """List all IP pools and blocks."""
    pools = network_service.ip_pools.list(
        db=db, ip_version=None, is_active=None, order_by="name", order_dir="asc", limit=100, offset=0
    )
    blocks = network_service.ip_blocks.list(
        db=db, pool_id=None, is_active=True, order_by="cidr", order_dir="asc", limit=100, offset=0
    )

    stats = {
        "total_pools": len(pools),
        "total_blocks": len(blocks),
        "ipv4_pools": sum(1 for p in pools if p.ip_version.value == "ipv4"),
        "ipv6_pools": sum(1 for p in pools if p.ip_version.value == "ipv6"),
    }

    context = _base_context(request, db, active_page="ip-pools", active_menu="ip-address")
    context.update({"pools": pools, "blocks": blocks, "stats": stats})
    return templates.TemplateResponse("admin/network/ip-management/pools.html", context)


@router.get("/vlans", response_class=HTMLResponse)
def vlans_list(request: Request, db: Session = Depends(get_db)):
    """List all VLANs."""
    vlans = network_service.vlans.list(
        db=db, region_id=None, is_active=True, order_by="tag", order_dir="asc", limit=100, offset=0
    )

    stats = {"total": len(vlans)}

    context = _base_context(request, db, active_page="vlans")
    context.update({"vlans": vlans, "stats": stats})
    return templates.TemplateResponse("admin/network/vlans/index.html", context)


@router.get("/vlans/new", response_class=HTMLResponse)
def vlan_new(request: Request, db: Session = Depends(get_db)):
    from app.services import catalog as catalog_service

    # Get regions for dropdown
    regions = catalog_service.region_zones.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )

    context = _base_context(request, db, active_page="vlans")
    context.update({
        "vlan": None,
        "regions": regions,
        "action_url": "/admin/network/vlans",
    })
    return templates.TemplateResponse("admin/network/vlans/form.html", context)


@router.get("/vlans/{vlan_id}", response_class=HTMLResponse)
def vlan_detail(request: Request, vlan_id: str, db: Session = Depends(get_db)):
    try:
        vlan = network_service.vlans.get(db=db, vlan_id=vlan_id)
    except Exception:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "VLAN not found"},
            status_code=404,
        )

    # Get port links for this VLAN
    from app.models.network import PortVlan
    port_links = db.query(PortVlan).filter(PortVlan.vlan_id == vlan.id).all()

    activities = _build_audit_activities(db, "vlan", str(vlan_id))
    context = _base_context(request, db, active_page="vlans")
    context.update({
        "vlan": vlan,
        "port_links": port_links,
        "activities": activities,
    })
    return templates.TemplateResponse("admin/network/vlans/detail.html", context)


@router.get("/radius", response_class=HTMLResponse)
def radius_page(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="radius")
    servers = radius_service.radius_servers.list(
        db=db,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    clients = radius_service.radius_clients.list(
        db=db,
        server_id=None,
        is_active=None,
        order_by="client_ip",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    profiles = catalog_service.radius_profiles.list(
        db=db,
        vendor=None,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    activities = _build_audit_activities_for_types(
        db,
        ["radius_server", "radius_client", "radius_profile"],
        limit=10,
    )
    context.update({
        "profiles": profiles,
        "servers": servers,
        "clients": clients,
        "activities": activities,
    })
    return templates.TemplateResponse("admin/network/radius/index.html", context)


@router.get("/radius/servers/new", response_class=HTMLResponse)
def radius_server_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="radius")
    context.update({
        "server": None,
        "action_url": "/admin/network/radius/servers",
    })
    return templates.TemplateResponse("admin/network/radius/server_form.html", context)


@router.get("/radius/servers", response_class=HTMLResponse)
def radius_servers_redirect():
    return RedirectResponse("/admin/network/radius", status_code=303)


@router.get("/radius/servers/{server_id}/edit", response_class=HTMLResponse)
def radius_server_edit(request: Request, server_id: str, db: Session = Depends(get_db)):
    try:
        server = radius_service.radius_servers.get(db=db, server_id=server_id)
    except Exception:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS server not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    context = _base_context(request, db, active_page="radius")
    context.update({
        "server": server,
        "action_url": f"/admin/network/radius/servers/{server_id}",
    })
    return templates.TemplateResponse("admin/network/radius/server_form.html", context)


@router.post("/radius/servers", response_class=HTMLResponse)
async def radius_server_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    name = form.get("name", "").strip()
    host = form.get("host", "").strip()
    auth_port_raw = form.get("auth_port", "").strip()
    acct_port_raw = form.get("acct_port", "").strip()
    description = form.get("description", "").strip()
    is_active = form.get("is_active") == "true"

    error = None
    if not name:
        error = "Server name is required."
    elif not host:
        error = "Host is required."

    auth_port = 1812
    acct_port = 1813
    if not error:
        try:
            auth_port = int(auth_port_raw) if auth_port_raw else 1812
            acct_port = int(acct_port_raw) if acct_port_raw else 1813
        except ValueError:
            error = "Auth and accounting ports must be valid integers."

    server_data = {
        "name": name,
        "host": host,
        "auth_port": auth_port_raw or "1812",
        "acct_port": acct_port_raw or "1813",
        "description": description or None,
        "is_active": is_active,
    }

    if error:
        context = _base_context(request, db, active_page="radius")
        context.update({
            "server": server_data,
            "action_url": "/admin/network/radius/servers",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/radius/server_form.html", context)

    try:
        payload = RadiusServerCreate(
            name=name,
            host=host,
            auth_port=auth_port,
            acct_port=acct_port,
            description=description or None,
            is_active=is_active,
        )
        server = radius_service.radius_servers.create(db=db, payload=payload)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="radius_server",
            entity_id=str(server.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"name": server.name, "host": server.host},
        )
        return RedirectResponse("/admin/network/radius", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="radius")
    context.update({
        "server": server_data,
        "action_url": "/admin/network/radius/servers",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/radius/server_form.html", context)


@router.post("/radius/servers/{server_id}", response_class=HTMLResponse)
async def radius_server_update(request: Request, server_id: str, db: Session = Depends(get_db)):
    try:
        server = radius_service.radius_servers.get(db=db, server_id=server_id)
    except Exception:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS server not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    before_snapshot = model_to_dict(server)
    form = await request.form()
    name = form.get("name", "").strip()
    host = form.get("host", "").strip()
    auth_port_raw = form.get("auth_port", "").strip()
    acct_port_raw = form.get("acct_port", "").strip()
    description = form.get("description", "").strip()
    is_active = form.get("is_active") == "true"

    error = None
    if not name:
        error = "Server name is required."
    elif not host:
        error = "Host is required."

    auth_port = server.auth_port
    acct_port = server.acct_port
    if not error:
        try:
            if auth_port_raw:
                auth_port = int(auth_port_raw)
            if acct_port_raw:
                acct_port = int(acct_port_raw)
        except ValueError:
            error = "Auth and accounting ports must be valid integers."

    server_data = {
        "id": server.id,
        "name": name,
        "host": host,
        "auth_port": auth_port_raw or str(server.auth_port),
        "acct_port": acct_port_raw or str(server.acct_port),
        "description": description or None,
        "is_active": is_active,
    }

    if error:
        context = _base_context(request, db, active_page="radius")
        context.update({
            "server": server_data,
            "action_url": f"/admin/network/radius/servers/{server_id}",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/radius/server_form.html", context)

    try:
        payload = RadiusServerUpdate(
            name=name,
            host=host,
            auth_port=auth_port,
            acct_port=acct_port,
            description=description or None,
            is_active=is_active,
        )
        updated_server = radius_service.radius_servers.update(
            db=db,
            server_id=server_id,
            payload=payload,
        )
        after_snapshot = model_to_dict(updated_server)
        changes = diff_dicts(before_snapshot, after_snapshot)
        metadata = {"changes": changes} if changes else None
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="radius_server",
            entity_id=str(updated_server.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata,
        )
        return RedirectResponse("/admin/network/radius", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="radius")
    context.update({
        "server": server_data,
        "action_url": f"/admin/network/radius/servers/{server_id}",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/radius/server_form.html", context)


@router.get("/radius/clients/new", response_class=HTMLResponse)
def radius_client_new(request: Request, db: Session = Depends(get_db)):
    from app.models.radius import RadiusServer
    servers = db.query(RadiusServer).filter(RadiusServer.is_active == True).order_by(RadiusServer.name).all()

    context = _base_context(request, db, active_page="radius")
    context.update({
        "client": None,
        "servers": servers,
        "action_url": "/admin/network/radius/clients",
    })
    return templates.TemplateResponse("admin/network/radius/client_form.html", context)


@router.get("/radius/clients/{client_id}/edit", response_class=HTMLResponse)
def radius_client_edit(request: Request, client_id: str, db: Session = Depends(get_db)):
    from app.models.radius import RadiusServer

    servers = db.query(RadiusServer).filter(RadiusServer.is_active == True).order_by(RadiusServer.name).all()

    try:
        client = radius_service.radius_clients.get(db=db, client_id=client_id)
    except Exception:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS client not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    context = _base_context(request, db, active_page="radius")
    context.update({
        "client": client,
        "servers": servers,
        "action_url": f"/admin/network/radius/clients/{client_id}",
    })
    return templates.TemplateResponse("admin/network/radius/client_form.html", context)


@router.post("/radius/clients", response_class=HTMLResponse)
async def radius_client_create(request: Request, db: Session = Depends(get_db)):
    from app.models.radius import RadiusServer

    servers = db.query(RadiusServer).filter(RadiusServer.is_active == True).order_by(RadiusServer.name).all()

    form = await request.form()
    server_id = form.get("server_id", "").strip()
    client_ip = form.get("client_ip", "").strip()
    shared_secret = form.get("shared_secret", "")
    description = form.get("description", "").strip()
    is_active = form.get("is_active") == "true"

    error = None
    if not server_id:
        error = "RADIUS server is required."
    elif not client_ip:
        error = "Client IP address is required."
    elif not shared_secret:
        error = "Shared secret is required."

    client_data = {
        "server_id": server_id,
        "client_ip": client_ip,
        "description": description or None,
        "is_active": is_active,
    }

    if error:
        context = _base_context(request, db, active_page="radius")
        context.update({
            "client": client_data,
            "servers": servers,
            "action_url": "/admin/network/radius/clients",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/radius/client_form.html", context)

    try:
        payload = RadiusClientCreate(
            server_id=server_id,
            client_ip=client_ip,
            shared_secret_hash=hashlib.sha256(shared_secret.encode("utf-8")).hexdigest(),
            description=description or None,
            is_active=is_active,
        )
        client = radius_service.radius_clients.create(db=db, payload=payload)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="radius_client",
            entity_id=str(client.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"client_ip": client.client_ip, "server_id": str(client.server_id)},
        )
        return RedirectResponse("/admin/network/radius", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="radius")
    context.update({
        "client": client_data,
        "servers": servers,
        "action_url": "/admin/network/radius/clients",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/radius/client_form.html", context)


@router.post("/radius/clients/{client_id}", response_class=HTMLResponse)
async def radius_client_update(request: Request, client_id: str, db: Session = Depends(get_db)):
    from app.models.radius import RadiusServer

    servers = db.query(RadiusServer).filter(RadiusServer.is_active == True).order_by(RadiusServer.name).all()

    try:
        client = radius_service.radius_clients.get(db=db, client_id=client_id)
    except Exception:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS client not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    before_snapshot = model_to_dict(client, exclude=RADIUS_CLIENT_EXCLUDE_FIELDS)
    form = await request.form()
    server_id = form.get("server_id", "").strip()
    client_ip = form.get("client_ip", "").strip()
    shared_secret = form.get("shared_secret", "")
    description = form.get("description", "").strip()
    is_active = form.get("is_active") == "true"

    error = None
    if not server_id:
        error = "RADIUS server is required."
    elif not client_ip:
        error = "Client IP address is required."

    client_data = {
        "id": client.id,
        "server_id": server_id,
        "client_ip": client_ip,
        "description": description or None,
        "is_active": is_active,
    }

    if error:
        context = _base_context(request, db, active_page="radius")
        context.update({
            "client": client_data,
            "servers": servers,
            "action_url": f"/admin/network/radius/clients/{client_id}",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/radius/client_form.html", context)

    payload_data = {
        "server_id": server_id,
        "client_ip": client_ip,
        "description": description or None,
        "is_active": is_active,
    }
    if shared_secret:
        payload_data["shared_secret_hash"] = hashlib.sha256(
            shared_secret.encode("utf-8")
        ).hexdigest()

    try:
        payload = RadiusClientUpdate(**payload_data)
        updated_client = radius_service.radius_clients.update(
            db=db,
            client_id=client_id,
            payload=payload,
        )
        after_snapshot = model_to_dict(updated_client, exclude=RADIUS_CLIENT_EXCLUDE_FIELDS)
        changes = diff_dicts(before_snapshot, after_snapshot)
        metadata = {"changes": changes} if changes else None
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="radius_client",
            entity_id=str(updated_client.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata,
        )
        return RedirectResponse("/admin/network/radius", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="radius")
    context.update({
        "client": client_data,
        "servers": servers,
        "action_url": f"/admin/network/radius/clients/{client_id}",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/radius/client_form.html", context)


@router.get("/radius/profiles/new", response_class=HTMLResponse)
def radius_profile_new(request: Request, db: Session = Depends(get_db)):
    from app.models.catalog import NasVendor

    context = _base_context(request, db, active_page="radius")
    context.update({
        "profile": None,
        "attributes": [],
        "vendors": [item.value for item in NasVendor],
        "action_url": "/admin/network/radius/profiles",
    })
    return templates.TemplateResponse("admin/network/radius/profile_form.html", context)


def _parse_radius_attributes(form) -> tuple[list[dict], str | None]:
    names = form.getlist("attribute_name")
    operators = form.getlist("attribute_operator")
    values = form.getlist("attribute_value")
    max_len = max(len(names), len(operators), len(values))
    attrs = []
    for idx in range(max_len):
        name = (names[idx] if idx < len(names) else "").strip()
        value = (values[idx] if idx < len(values) else "").strip()
        operator = (operators[idx] if idx < len(operators) else "").strip() or None
        if not name and not value:
            continue
        if not name or not value:
            return [], "Each RADIUS attribute row needs both an attribute and a value."
        attrs.append({"attribute": name, "operator": operator, "value": value})
    return attrs, None


@router.get("/radius/profiles/{profile_id}/edit", response_class=HTMLResponse)
def radius_profile_edit(request: Request, profile_id: str, db: Session = Depends(get_db)):
    from app.models.catalog import NasVendor

    try:
        profile = catalog_service.radius_profiles.get(db=db, profile_id=profile_id)
    except Exception:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS profile not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    attributes = catalog_service.radius_attributes.list(
        db=db,
        profile_id=profile_id,
        order_by="attribute",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    context = _base_context(request, db, active_page="radius")
    context.update({
        "profile": profile,
        "attributes": attributes,
        "vendors": [item.value for item in NasVendor],
        "action_url": f"/admin/network/radius/profiles/{profile_id}",
    })
    return templates.TemplateResponse("admin/network/radius/profile_form.html", context)


@router.post("/radius/profiles", response_class=HTMLResponse)
async def radius_profile_create(request: Request, db: Session = Depends(get_db)):
    from app.models.catalog import NasVendor

    form = await request.form()
    name = form.get("name", "").strip()
    vendor = form.get("vendor", "").strip()
    description = form.get("description", "").strip()
    is_active = form.get("is_active") == "true"
    attributes, attr_error = _parse_radius_attributes(form)

    error = None
    if not name:
        error = "Profile name is required."
    elif attr_error:
        error = attr_error

    profile_data = {
        "name": name,
        "description": description or None,
        "is_active": is_active,
    }
    if vendor:
        profile_data["vendor"] = vendor

    if error:
        context = _base_context(request, db, active_page="radius")
        context.update({
            "profile": profile_data,
            "attributes": attributes,
            "vendors": [item.value for item in NasVendor],
            "action_url": "/admin/network/radius/profiles",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/radius/profile_form.html", context)

    payload = RadiusProfileCreate(**profile_data)
    profile = catalog_service.radius_profiles.create(db=db, payload=payload)
    for attr in attributes:
        catalog_service.radius_attributes.create(
            db=db,
            payload=RadiusAttributeCreate(profile_id=profile.id, **attr),
        )
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="radius_profile",
        entity_id=str(profile.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={
            "name": profile.name,
            "vendor": profile.vendor.value if profile.vendor else None,
            "attributes": {"from": 0, "to": len(attributes)},
        },
    )
    return RedirectResponse("/admin/network/radius", status_code=303)


@router.post("/radius/profiles/{profile_id}", response_class=HTMLResponse)
async def radius_profile_update(request: Request, profile_id: str, db: Session = Depends(get_db)):
    from app.models.catalog import NasVendor

    try:
        profile = catalog_service.radius_profiles.get(db=db, profile_id=profile_id)
    except Exception:
        context = _base_context(request, db, active_page="radius")
        context.update({"message": "RADIUS profile not found"})
        return templates.TemplateResponse(
            "admin/errors/404.html",
            context,
            status_code=404,
        )

    before_snapshot = model_to_dict(profile)
    form = await request.form()
    name = form.get("name", "").strip()
    vendor = form.get("vendor", "").strip()
    description = form.get("description", "").strip()
    is_active = form.get("is_active") == "true"
    attributes, attr_error = _parse_radius_attributes(form)

    error = None
    if not name:
        error = "Profile name is required."
    elif attr_error:
        error = attr_error

    profile_data = {
        "name": name,
        "description": description or None,
        "is_active": is_active,
    }
    if vendor:
        profile_data["vendor"] = vendor

    if error:
        context = _base_context(request, db, active_page="radius")
        context.update({
            "profile": profile_data,
            "attributes": attributes,
            "vendors": [item.value for item in NasVendor],
            "action_url": f"/admin/network/radius/profiles/{profile_id}",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/radius/profile_form.html", context)

    existing = catalog_service.radius_attributes.list(
        db=db,
        profile_id=profile_id,
        order_by="attribute",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    before_attr_count = len(existing)

    payload = RadiusProfileUpdate(**profile_data)
    updated_profile = catalog_service.radius_profiles.update(
        db=db,
        profile_id=profile_id,
        payload=payload,
    )
    for attr in existing:
        catalog_service.radius_attributes.delete(db=db, attribute_id=str(attr.id))
    for attr in attributes:
        catalog_service.radius_attributes.create(
            db=db,
            payload=RadiusAttributeCreate(profile_id=profile.id, **attr),
        )
    after_snapshot = model_to_dict(updated_profile)
    changes = diff_dicts(before_snapshot, after_snapshot)
    after_attr_count = len(attributes)
    if before_attr_count != after_attr_count:
        changes["attributes"] = {"from": before_attr_count, "to": after_attr_count}
    metadata = {"changes": changes} if changes else None
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="radius_profile",
        entity_id=str(updated_profile.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata,
    )
    return RedirectResponse("/admin/network/radius", status_code=303)


@router.get("/monitoring", response_class=HTMLResponse)
def monitoring_page(request: Request, db: Session = Depends(get_db)):
    from app.models.network_monitoring import (
        Alert,
        AlertEvent,
        AlertStatus,
        DeviceMetric,
        DeviceStatus,
        MetricType,
        NetworkDevice,
    )
    from app.models.usage import AccountingStatus, RadiusAccountingSession

    devices = (
        db.query(NetworkDevice)
        .filter(NetworkDevice.is_active.is_(True))
        .order_by(NetworkDevice.name)
        .all()
    )
    online_statuses = {DeviceStatus.online, DeviceStatus.degraded, DeviceStatus.maintenance}
    devices_online = sum(1 for d in devices if d.status in online_statuses)
    devices_offline = sum(1 for d in devices if d.status == DeviceStatus.offline)

    alerts = (
        db.query(Alert)
        .filter(Alert.status.in_([AlertStatus.open, AlertStatus.acknowledged]))
        .order_by(Alert.triggered_at.desc())
        .limit(10)
        .all()
    )
    active_alarm_count = sum(1 for a in alerts if a.status == AlertStatus.open)

    recent_events = (
        db.query(AlertEvent)
        .order_by(AlertEvent.created_at.desc())
        .limit(10)
        .all()
    )

    online_subscribers = (
        db.query(func.count(func.distinct(RadiusAccountingSession.subscription_id)))
        .filter(RadiusAccountingSession.session_end.is_(None))
        .filter(RadiusAccountingSession.status_type != AccountingStatus.stop)
        .scalar()
        or 0
    )

    metric_types = [
        MetricType.cpu,
        MetricType.memory,
        MetricType.uptime,
        MetricType.rx_bps,
        MetricType.tx_bps,
    ]
    latest_metrics_subq = (
        db.query(
            DeviceMetric.device_id,
            DeviceMetric.metric_type,
            func.max(DeviceMetric.recorded_at).label("latest"),
        )
        .filter(DeviceMetric.metric_type.in_(metric_types))
        .group_by(DeviceMetric.device_id, DeviceMetric.metric_type)
        .subquery()
    )
    latest_metrics = (
        db.query(DeviceMetric)
        .join(
            latest_metrics_subq,
            and_(
                DeviceMetric.device_id == latest_metrics_subq.c.device_id,
                DeviceMetric.metric_type == latest_metrics_subq.c.metric_type,
                DeviceMetric.recorded_at == latest_metrics_subq.c.latest,
            ),
        )
        .all()
    )

    metrics_by_device: dict[str, dict[MetricType, DeviceMetric]] = {}
    rx_total = 0.0
    tx_total = 0.0
    cpu_values = []
    mem_values = []
    for metric in latest_metrics:
        device_metrics = metrics_by_device.setdefault(str(metric.device_id), {})
        device_metrics[metric.metric_type] = metric
        if metric.metric_type == MetricType.rx_bps:
            rx_total += float(metric.value or 0)
        elif metric.metric_type == MetricType.tx_bps:
            tx_total += float(metric.value or 0)
        elif metric.metric_type == MetricType.cpu:
            cpu_values.append(float(metric.value or 0))
        elif metric.metric_type == MetricType.memory:
            mem_values.append(float(metric.value or 0))

    avg_cpu = sum(cpu_values) / len(cpu_values) if cpu_values else None
    avg_mem = sum(mem_values) / len(mem_values) if mem_values else None

    device_health = []
    for device in devices:
        device_metrics = metrics_by_device.get(str(device.id), {})
        cpu_metric = device_metrics.get(MetricType.cpu)
        mem_metric = device_metrics.get(MetricType.memory)
        uptime_metric = device_metrics.get(MetricType.uptime)
        device_health.append({
            "name": device.name,
            "status": device.status.value if device.status else "unknown",
            "cpu": f"{cpu_metric.value:.1f}%" if cpu_metric else "--",
            "memory": f"{mem_metric.value:.1f}%" if mem_metric else "--",
            "uptime": _format_duration(uptime_metric.value if uptime_metric else None),
            "last_seen": device.last_ping_at or device.last_snmp_at,
        })

    context = _base_context(request, db, active_page="monitoring")
    context.update({
        "stats": {
            "devices_online": devices_online,
            "devices_offline": devices_offline,
            "alarms_open": active_alarm_count,
            "subscribers_online": online_subscribers,
        },
        "alarms": alerts,
        "recent_events": recent_events,
        "performance": {
            "avg_cpu": f"{avg_cpu:.1f}%" if avg_cpu is not None else "--",
            "avg_memory": f"{avg_mem:.1f}%" if avg_mem is not None else "--",
            "rx_bps": _format_bps(rx_total) if rx_total > 0 else "--",
            "tx_bps": _format_bps(tx_total) if tx_total > 0 else "--",
        },
        "device_health": device_health,
        "activities": _build_audit_activities_for_types(
            db,
            ["core_device", "network_device"],
            limit=5,
        ),
    })
    return templates.TemplateResponse("admin/network/monitoring/index.html", context)


@router.get("/alarms", response_class=HTMLResponse)
def alarms_page(
    request: Request,
    severity: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    from app.models.network_monitoring import Alert, AlertRule, AlertStatus, AlertSeverity

    # Get open and acknowledged alarms
    alarms_query = db.query(Alert).order_by(Alert.triggered_at.desc())
    if status:
        try:
            alarms_query = alarms_query.filter(Alert.status == AlertStatus(status))
        except ValueError:
            pass
    else:
        alarms_query = alarms_query.filter(Alert.status.in_([AlertStatus.open, AlertStatus.acknowledged]))
    if severity:
        try:
            alarms_query = alarms_query.filter(Alert.severity == AlertSeverity(severity))
        except ValueError:
            pass
    alarms = alarms_query.limit(100).all()

    # Get alert rules
    rules = db.query(AlertRule).filter(AlertRule.is_active == True).order_by(AlertRule.name).all()

    # Calculate stats
    stats = {
        "critical": sum(1 for a in alarms if a.severity == AlertSeverity.critical and a.status == AlertStatus.open),
        "warning": sum(1 for a in alarms if a.severity == AlertSeverity.warning and a.status == AlertStatus.open),
        "info": sum(1 for a in alarms if a.severity == AlertSeverity.info and a.status == AlertStatus.open),
        "total_open": sum(1 for a in alarms if a.status == AlertStatus.open),
    }

    context = _base_context(request, db, active_page="monitoring")
    context.update({
        "alarms": alarms,
        "rules": rules,
        "stats": stats,
        "severity": severity,
        "status": status,
    })
    return templates.TemplateResponse("admin/network/monitoring/alarms.html", context)


# ==================== POP Sites ====================

@router.get("/pop-sites", response_class=HTMLResponse)
def pop_sites_list(request: Request, status: str | None = None, db: Session = Depends(get_db)):
    """List all POP sites."""
    from app.models.network_monitoring import PopSite

    query = db.query(PopSite).order_by(PopSite.name)
    status_filter = (status or "all").strip().lower()
    if status_filter == "active":
        query = query.filter(PopSite.is_active == True)
    elif status_filter == "inactive":
        query = query.filter(PopSite.is_active == False)

    pop_sites = query.limit(100).all()

    all_sites = db.query(PopSite).all()
    stats = {
        "total": len(all_sites),
        "active": sum(1 for p in all_sites if p.is_active),
        "inactive": sum(1 for p in all_sites if not p.is_active),
    }

    context = _base_context(request, db, active_page="pop-sites")
    context.update({"pop_sites": pop_sites, "stats": stats, "status_filter": status_filter})
    return templates.TemplateResponse("admin/network/pop-sites/index.html", context)


@router.get("/pop-sites/new", response_class=HTMLResponse)
def pop_site_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="pop-sites")
    context.update({
        "pop_site": None,
        "action_url": "/admin/network/pop-sites",
        "mast_enabled": False,
        "mast_defaults": _default_mast_context(),
    })
    return templates.TemplateResponse("admin/network/pop-sites/form.html", context)


@router.post("/pop-sites", response_class=HTMLResponse)
async def pop_site_create(request: Request, db: Session = Depends(get_db)):
    from app.models.network_monitoring import PopSite
    from app.schemas.wireless_mast import WirelessMastCreate
    from app.services import wireless_mast as wireless_mast_service

    form = await request.form()
    name = form.get("name", "").strip()
    code = form.get("code", "").strip() or None
    address_line1 = form.get("address_line1", "").strip() or None
    address_line2 = form.get("address_line2", "").strip() or None
    city = form.get("city", "").strip() or None
    region = form.get("region", "").strip() or None
    postal_code = form.get("postal_code", "").strip() or None
    country_code = form.get("country_code", "").strip() or None
    latitude = form.get("latitude", "").strip() or None
    longitude = form.get("longitude", "").strip() or None
    notes = form.get("notes", "").strip() or None
    is_active = form.get("is_active") == "true"
    lat_value = float(latitude) if latitude else None
    lon_value = float(longitude) if longitude else None

    if not name:
        mast_enabled, _, _, mast_defaults = _parse_mast_form(form, lat_value, lon_value)
        context = _base_context(request, db, active_page="pop-sites")
        context.update({
            "pop_site": None,
            "action_url": "/admin/network/pop-sites",
            "error": "Site name is required",
            "mast_enabled": mast_enabled,
            "mast_defaults": mast_defaults,
        })
        return templates.TemplateResponse("admin/network/pop-sites/form.html", context)

    mast_enabled, mast_data, mast_error, mast_defaults = _parse_mast_form(
        form, lat_value, lon_value
    )
    if mast_error:
        context = _base_context(request, db, active_page="pop-sites")
        context.update({
            "pop_site": None,
            "action_url": "/admin/network/pop-sites",
            "error": None,
            "mast_error": mast_error,
            "mast_enabled": mast_enabled,
            "mast_defaults": mast_defaults,
        })
        return templates.TemplateResponse("admin/network/pop-sites/form.html", context)

    pop_site = PopSite(
        name=name,
        code=code,
        address_line1=address_line1,
        address_line2=address_line2,
        city=city,
        region=region,
        postal_code=postal_code,
        country_code=country_code,
        latitude=lat_value,
        longitude=lon_value,
        notes=notes,
        is_active=is_active,
    )
    db.add(pop_site)
    db.commit()
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="pop_site",
        entity_id=str(pop_site.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": pop_site.name, "code": pop_site.code},
    )

    if mast_enabled and mast_data:
        mast_payload = WirelessMastCreate(**mast_data, pop_site_id=pop_site.id)
        wireless_mast_service.wireless_masts.create(db, mast_payload)

    return RedirectResponse(f"/admin/network/pop-sites/{pop_site.id}", status_code=303)


@router.get("/pop-sites/{pop_site_id}/edit", response_class=HTMLResponse)
def pop_site_edit(request: Request, pop_site_id: str, db: Session = Depends(get_db)):
    from app.models.network_monitoring import PopSite

    pop_site = db.query(PopSite).filter(PopSite.id == pop_site_id).first()
    if not pop_site:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "POP Site not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="pop-sites")
    context.update({
        "pop_site": pop_site,
        "action_url": f"/admin/network/pop-sites/{pop_site.id}",
        "mast_enabled": False,
        "mast_defaults": _default_mast_context(),
    })
    return templates.TemplateResponse("admin/network/pop-sites/form.html", context)


@router.post("/pop-sites/{pop_site_id}", response_class=HTMLResponse)
async def pop_site_update(request: Request, pop_site_id: str, db: Session = Depends(get_db)):
    from app.models.network_monitoring import PopSite
    from app.schemas.wireless_mast import WirelessMastCreate
    from app.services import wireless_mast as wireless_mast_service

    pop_site = db.query(PopSite).filter(PopSite.id == pop_site_id).first()
    if not pop_site:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "POP Site not found"},
            status_code=404,
        )

    form = await request.form()
    name = form.get("name", "").strip()

    if not name:
        mast_enabled, _, _, mast_defaults = _parse_mast_form(form, pop_site.latitude, pop_site.longitude)
        context = _base_context(request, db, active_page="pop-sites")
        context.update({
            "pop_site": pop_site,
            "action_url": f"/admin/network/pop-sites/{pop_site.id}",
            "error": "Site name is required",
            "mast_enabled": mast_enabled,
            "mast_defaults": mast_defaults,
        })
        return templates.TemplateResponse("admin/network/pop-sites/form.html", context)

    before_snapshot = model_to_dict(pop_site)
    pop_site.name = name
    pop_site.code = form.get("code", "").strip() or None
    pop_site.address_line1 = form.get("address_line1", "").strip() or None
    pop_site.address_line2 = form.get("address_line2", "").strip() or None
    pop_site.city = form.get("city", "").strip() or None
    pop_site.region = form.get("region", "").strip() or None
    pop_site.postal_code = form.get("postal_code", "").strip() or None
    pop_site.country_code = form.get("country_code", "").strip() or None
    latitude = form.get("latitude", "").strip()
    longitude = form.get("longitude", "").strip()
    pop_site.latitude = float(latitude) if latitude else None
    pop_site.longitude = float(longitude) if longitude else None
    pop_site.notes = form.get("notes", "").strip() or None
    pop_site.is_active = form.get("is_active") == "true"

    mast_enabled, mast_data, mast_error, mast_defaults = _parse_mast_form(
        form, pop_site.latitude, pop_site.longitude
    )
    if mast_error:
        context = _base_context(request, db, active_page="pop-sites")
        context.update({
            "pop_site": pop_site,
            "action_url": f"/admin/network/pop-sites/{pop_site.id}",
            "mast_error": mast_error,
            "mast_enabled": mast_enabled,
            "mast_defaults": mast_defaults,
        })
        return templates.TemplateResponse("admin/network/pop-sites/form.html", context)

    db.commit()
    after_snapshot = model_to_dict(pop_site)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata_payload = {"changes": changes} if changes else None
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="pop_site",
        entity_id=str(pop_site.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata_payload,
    )

    if mast_enabled and mast_data:
        mast_payload = WirelessMastCreate(**mast_data, pop_site_id=pop_site.id)
        wireless_mast_service.wireless_masts.create(db, mast_payload)

    return RedirectResponse(f"/admin/network/pop-sites/{pop_site.id}", status_code=303)


@router.get("/pop-sites/{pop_site_id}", response_class=HTMLResponse)
def pop_site_detail(request: Request, pop_site_id: str, db: Session = Depends(get_db)):
    from app.models.network_monitoring import PopSite, NetworkDevice
    from app.models.wireless_mast import WirelessMast

    pop_site = db.query(PopSite).filter(PopSite.id == pop_site_id).first()
    if not pop_site:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "POP Site not found"},
            status_code=404,
        )

    # Get devices at this site
    devices = db.query(NetworkDevice).filter(NetworkDevice.pop_site_id == pop_site.id).order_by(NetworkDevice.name).all()
    masts = (
        db.query(WirelessMast)
        .filter(WirelessMast.pop_site_id == pop_site.id)
        .order_by(WirelessMast.name)
        .all()
    )

    activities = _build_audit_activities(db, "pop_site", str(pop_site_id))
    context = _base_context(request, db, active_page="pop-sites")
    context.update({
        "pop_site": pop_site,
        "devices": devices,
        "masts": masts,
        "activities": activities,
    })
    return templates.TemplateResponse("admin/network/pop-sites/detail.html", context)


# ==================== Network Devices (Consolidated) ====================

@router.get("/network-devices", response_class=HTMLResponse)
def network_devices_consolidated(
    request: Request,
    tab: str = "core",
    db: Session = Depends(get_db),
):
    """Consolidated view of all network devices - core, OLTs, ONTs/CPE."""
    from app.models.network_monitoring import NetworkDevice, DeviceRole
    from app.models.network import CPEDevice

    # Core devices
    core_devices = db.query(NetworkDevice).order_by(NetworkDevice.name).limit(200).all()
    core_roles = {
        "core": sum(1 for d in core_devices if d.role and d.role.value == "core"),
        "distribution": sum(1 for d in core_devices if d.role and d.role.value == "distribution"),
        "access": sum(1 for d in core_devices if d.role and d.role.value == "access"),
        "aggregation": sum(1 for d in core_devices if d.role and d.role.value == "aggregation"),
        "edge": sum(1 for d in core_devices if d.role and d.role.value == "edge"),
    }

    # OLTs
    olts = network_service.olt_devices.list(
        db=db,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    olt_stats = {}
    for olt in olts:
        pon_ports = network_service.pon_ports.list(
            db=db,
            olt_id=str(olt.id),
            is_active=True,
            order_by="created_at",
            order_dir="asc",
            limit=1000,
            offset=0,
        )
        olt_stats[str(olt.id)] = {"pon_ports": len(pon_ports)}

    # ONTs
    active_onts = network_service.ont_units.list(
        db=db,
        is_active=True,
        order_by="serial_number",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    inactive_onts = network_service.ont_units.list(
        db=db,
        is_active=False,
        order_by="serial_number",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    onts = active_onts + inactive_onts

    # CPEs
    cpes = db.query(CPEDevice).order_by(CPEDevice.created_at.desc()).limit(200).all()

    stats = {
        "core_total": len(core_devices),
        "core_roles": core_roles,
        "olt_total": len(olts),
        "olt_active": sum(1 for o in olts if o.is_active),
        "ont_total": len(onts),
        "ont_inactive": len(inactive_onts),
        "cpe_total": len(cpes),
    }

    context = _base_context(request, db, active_page="network-devices")
    context.update({
        "tab": tab,
        "stats": stats,
        "core_devices": core_devices,
        "olts": olts,
        "olt_stats": olt_stats,
        "onts": onts,
        "cpes": cpes,
    })
    return templates.TemplateResponse("admin/network/network-devices/index.html", context)


# ==================== Core Network Devices ====================

def _core_device_integrity_error_message(exc: Exception) -> str:
    message = str(exc)
    if "uq_network_devices_hostname" in message:
        return "Hostname already exists"
    if "uq_network_devices_mgmt_ip" in message:
        return "Management IP already exists"
    return "Device could not be saved due to a data conflict"


@router.get("/core-devices", response_class=HTMLResponse)
def core_devices_list(
    request: Request,
    role: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    """List core network devices (routers, switches, access points, etc.)."""
    from app.models.network_monitoring import NetworkDevice, DeviceRole

    query = db.query(NetworkDevice)

    if role:
        try:
            role_enum = DeviceRole(role)
            query = query.filter(NetworkDevice.role == role_enum)
        except ValueError:
            pass

    status_filter = (status or "all").strip().lower()
    if status_filter == "active":
        query = query.filter(NetworkDevice.is_active == True)
    elif status_filter == "inactive":
        query = query.filter(NetworkDevice.is_active == False)

    devices = query.order_by(NetworkDevice.name).limit(200).all()

    stats = {
        "total": len(devices),
        "core": sum(1 for d in devices if d.role.value == "core"),
        "distribution": sum(1 for d in devices if d.role.value == "distribution"),
        "access": sum(1 for d in devices if d.role.value == "access"),
        "aggregation": sum(1 for d in devices if d.role.value == "aggregation"),
        "edge": sum(1 for d in devices if d.role.value == "edge"),
    }

    context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
    context.update({
        "devices": devices,
        "stats": stats,
        "role_filter": role,
        "status_filter": status_filter,
    })
    return templates.TemplateResponse("admin/network/core-devices/index.html", context)


@router.get("/core-devices/new", response_class=HTMLResponse)
def core_device_new(request: Request, db: Session = Depends(get_db)):
    from app.models.network_monitoring import PopSite

    pop_sites = db.query(PopSite).filter(PopSite.is_active == True).order_by(PopSite.name).all()

    context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
    context.update({
        "device": None,
        "pop_sites": pop_sites,
        "action_url": "/admin/network/core-devices",
    })
    return templates.TemplateResponse("admin/network/core-devices/form.html", context)


@router.post("/core-devices", response_class=HTMLResponse)
async def core_device_create(request: Request, db: Session = Depends(get_db)):
    from types import SimpleNamespace
    from app.models.network_monitoring import DeviceRole, DeviceStatus, DeviceType, NetworkDevice, PopSite
    from sqlalchemy.exc import IntegrityError

    form = await request.form()
    name = form.get("name", "").strip()
    hostname = form.get("hostname", "").strip() or None
    mgmt_ip = form.get("mgmt_ip", "").strip() or None
    role_value = form.get("role", "").strip()
    device_type_value = form.get("device_type", "").strip()
    pop_site_id = form.get("pop_site_id", "").strip() or None
    ping_enabled = form.get("ping_enabled") == "true"
    snmp_enabled = form.get("snmp_enabled") == "true"
    role = DeviceRole.edge
    device_type = None
    snmp_port = 161 if snmp_enabled else None

    pop_sites = db.query(PopSite).filter(PopSite.is_active == True).order_by(PopSite.name).all()

    def _error_response(message: str):
        device_snapshot = SimpleNamespace(
            name=name,
            hostname=hostname,
            mgmt_ip=mgmt_ip,
            role=role,
            status=DeviceStatus.offline,
            pop_site_id=pop_site_id,
            vendor=form.get("vendor", "").strip() or None,
            model=form.get("model", "").strip() or None,
            serial_number=form.get("serial_number", "").strip() or None,
            device_type=device_type,
            ping_enabled=ping_enabled,
            snmp_enabled=snmp_enabled,
            snmp_port=snmp_port,
            snmp_version=form.get("snmp_version", "").strip() or None,
            snmp_community=form.get("snmp_community", "").strip() or None,
            snmp_username=form.get("snmp_username", "").strip() or None,
            snmp_auth_protocol=form.get("snmp_auth_protocol", "").strip() or None,
            snmp_auth_secret=form.get("snmp_auth_secret", "").strip() or None,
            snmp_priv_protocol=form.get("snmp_priv_protocol", "").strip() or None,
            snmp_priv_secret=form.get("snmp_priv_secret", "").strip() or None,
            notes=form.get("notes", "").strip() or None,
            is_active=form.get("is_active") == "true",
        )
        context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
        context.update({
            "device": device_snapshot,
            "pop_sites": pop_sites,
            "action_url": "/admin/network/core-devices",
            "error": message,
        })
        return templates.TemplateResponse("admin/network/core-devices/form.html", context)

    if not name:
        return _error_response("Device name is required")

    try:
        role = DeviceRole(role_value)
    except ValueError:
        role = DeviceRole.edge
        return _error_response("Invalid device role")

    try:
        device_type = DeviceType(device_type_value) if device_type_value else None
    except ValueError:
        device_type = None
        return _error_response("Invalid device type")

    snmp_port_value = form.get("snmp_port", "").strip()
    if snmp_port_value:
        try:
            snmp_port = int(snmp_port_value)
        except ValueError:
            snmp_port = 161
            return _error_response("SNMP port must be a valid number")
    else:
        snmp_port = 161 if snmp_enabled else None

    if pop_site_id:
        pop_site = db.query(PopSite).filter(PopSite.id == pop_site_id).first()
        if not pop_site:
            return _error_response("Selected POP site was not found")

    if hostname:
        existing = (
            db.query(NetworkDevice)
            .filter(NetworkDevice.hostname == hostname)
            .first()
        )
        if existing:
            return _error_response("Hostname already exists")
    if mgmt_ip:
        existing = (
            db.query(NetworkDevice)
            .filter(NetworkDevice.mgmt_ip == mgmt_ip)
            .first()
        )
        if existing:
            return _error_response("Management IP already exists")

    host = mgmt_ip or hostname
    if ping_enabled and not host:
        return _error_response("Management IP or hostname is required for ping checks.")
    if snmp_enabled and not host:
        return _error_response("Management IP or hostname is required for SNMP checks.")

    device = NetworkDevice(
        name=name,
        hostname=hostname,
        mgmt_ip=mgmt_ip,
        role=role,
        status=DeviceStatus.offline,
        pop_site_id=pop_site_id,
        vendor=form.get("vendor", "").strip() or None,
        model=form.get("model", "").strip() or None,
        serial_number=form.get("serial_number", "").strip() or None,
        device_type=device_type,
        ping_enabled=ping_enabled,
        snmp_enabled=snmp_enabled,
        snmp_port=snmp_port,
        snmp_version=form.get("snmp_version", "").strip() or None,
        snmp_community=form.get("snmp_community", "").strip() or None,
        snmp_username=form.get("snmp_username", "").strip() or None,
        snmp_auth_protocol=form.get("snmp_auth_protocol", "").strip() or None,
        snmp_auth_secret=form.get("snmp_auth_secret", "").strip() or None,
        snmp_priv_protocol=form.get("snmp_priv_protocol", "").strip() or None,
        snmp_priv_secret=form.get("snmp_priv_secret", "").strip() or None,
        notes=form.get("notes", "").strip() or None,
        is_active=form.get("is_active") == "true",
    )

    if ping_enabled and host:
        try:
            result = subprocess.run(
                _build_ping_command(host),
                capture_output=True,
                text=True,
                check=False,
                timeout=4,
            )
            if result.returncode != 0:
                return _error_response("Ping failed. Check the management IP/hostname.")
            device.last_ping_at = datetime.now(timezone.utc)
            device.last_ping_ok = True
        except Exception:
            return _error_response("Ping failed. Check the management IP/hostname.")

    if snmp_enabled:
        try:
            from app.services.snmp_discovery import _run_snmpwalk

            _run_snmpwalk(device, ".1.3.6.1.2.1.1.3.0", timeout=8)
            device.last_snmp_at = datetime.now(timezone.utc)
            device.last_snmp_ok = True
        except Exception as exc:
            return _error_response(f"SNMP check failed: {str(exc)}")

    db.add(device)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        error = _core_device_integrity_error_message(exc)
        return _error_response(error)

    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="core_device",
        entity_id=str(device.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": device.name, "mgmt_ip": device.mgmt_ip or None},
    )
    return RedirectResponse(f"/admin/network/core-devices/{device.id}", status_code=303)


@router.get("/core-devices/{device_id}/edit", response_class=HTMLResponse)
def core_device_edit(request: Request, device_id: str, db: Session = Depends(get_db)):
    from app.models.network_monitoring import NetworkDevice, PopSite

    device = db.query(NetworkDevice).filter(NetworkDevice.id == device_id).first()
    if not device:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Device not found"},
            status_code=404,
        )
    before_snapshot = model_to_dict(device)

    pop_sites = db.query(PopSite).filter(PopSite.is_active == True).order_by(PopSite.name).all()

    context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
    context.update({
        "device": device,
        "pop_sites": pop_sites,
        "action_url": f"/admin/network/core-devices/{device.id}",
    })
    return templates.TemplateResponse("admin/network/core-devices/form.html", context)


@router.get("/core-devices/{device_id}", response_class=HTMLResponse)
def core_device_detail(request: Request, device_id: str, db: Session = Depends(get_db)):
    from app.models.network_monitoring import (
        Alert,
        AlertStatus,
        DeviceInterface,
        DeviceMetric,
        MetricType,
        NetworkDevice,
    )

    device = db.query(NetworkDevice).filter(NetworkDevice.id == device_id).first()
    if not device:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Device not found"},
            status_code=404,
        )

    interfaces = (
        db.query(DeviceInterface)
        .filter(DeviceInterface.device_id == device.id)
        .order_by(DeviceInterface.name)
        .all()
    )

    selected_interface = None
    selected_interface_id = request.query_params.get("interface_id")
    if selected_interface_id:
        selected_interface = (
            db.query(DeviceInterface)
            .filter(DeviceInterface.id == selected_interface_id, DeviceInterface.device_id == device.id)
            .first()
        )

    # Get active alerts for this device
    alerts = db.query(Alert).filter(
        Alert.device_id == device.id,
        Alert.status.in_([AlertStatus.open, AlertStatus.acknowledged])
    ).order_by(Alert.triggered_at.desc()).limit(10).all()

    metric_types = [
        MetricType.cpu,
        MetricType.memory,
        MetricType.uptime,
    ]
    if not selected_interface:
        metric_types.extend([MetricType.rx_bps, MetricType.tx_bps])
    latest_metrics_subq = (
        db.query(
            DeviceMetric.metric_type,
            func.max(DeviceMetric.recorded_at).label("latest"),
        )
        .filter(DeviceMetric.device_id == device.id)
        .filter(DeviceMetric.metric_type.in_(metric_types))
        .group_by(DeviceMetric.metric_type)
        .subquery()
    )
    latest_metrics = (
        db.query(DeviceMetric)
        .join(
            latest_metrics_subq,
            and_(
                DeviceMetric.metric_type == latest_metrics_subq.c.metric_type,
                DeviceMetric.recorded_at == latest_metrics_subq.c.latest,
            ),
        )
        .filter(DeviceMetric.device_id == device.id)
        .all()
    )
    metrics_by_type = {metric.metric_type: metric for metric in latest_metrics}
    cpu_metric = metrics_by_type.get(MetricType.cpu)
    mem_metric = metrics_by_type.get(MetricType.memory)
    uptime_metric = metrics_by_type.get(MetricType.uptime)
    rx_metric = None
    tx_metric = None
    if selected_interface:
        interface_metric_types = [MetricType.rx_bps, MetricType.tx_bps]
        interface_metrics_subq = (
            db.query(
                DeviceMetric.metric_type,
                func.max(DeviceMetric.recorded_at).label("latest"),
            )
            .filter(DeviceMetric.device_id == device.id)
            .filter(DeviceMetric.interface_id == selected_interface.id)
            .filter(DeviceMetric.metric_type.in_(interface_metric_types))
            .group_by(DeviceMetric.metric_type)
            .subquery()
        )
        interface_metrics = (
            db.query(DeviceMetric)
            .join(
                interface_metrics_subq,
                and_(
                    DeviceMetric.metric_type == interface_metrics_subq.c.metric_type,
                    DeviceMetric.recorded_at == interface_metrics_subq.c.latest,
                ),
            )
            .filter(DeviceMetric.device_id == device.id)
            .filter(DeviceMetric.interface_id == selected_interface.id)
            .all()
        )
        interface_metrics_by_type = {metric.metric_type: metric for metric in interface_metrics}
        rx_metric = interface_metrics_by_type.get(MetricType.rx_bps)
        tx_metric = interface_metrics_by_type.get(MetricType.tx_bps)
    else:
        rx_metric = metrics_by_type.get(MetricType.rx_bps)
        tx_metric = metrics_by_type.get(MetricType.tx_bps)

    activities = _build_audit_activities(db, "core_device", str(device_id))
    context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
    context.update({
        "device": device,
        "interfaces": interfaces,
        "selected_interface": selected_interface,
        "alerts": alerts,
        "device_health": {
            "cpu": f"{cpu_metric.value:.1f}%" if cpu_metric else "--",
            "memory": f"{mem_metric.value:.1f}%" if mem_metric else "--",
            "uptime": _format_duration(uptime_metric.value if uptime_metric else None),
            "rx": _format_bps(rx_metric.value) if rx_metric else "--",
            "tx": _format_bps(tx_metric.value) if tx_metric else "--",
            "last_seen": device.last_ping_at or device.last_snmp_at,
        },
        "activities": activities,
    })
    return templates.TemplateResponse("admin/network/core-devices/detail.html", context)


def _render_device_status_badge(status_value: str) -> str:
    if status_value == "online":
        return (
            '<span class="inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm font-medium '
            'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400">'
            '<span class="h-2 w-2 rounded-full bg-green-500"></span>'
            "Online</span>"
        )
    if status_value == "offline":
        return (
            '<span class="inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm font-medium '
            'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400">'
            '<span class="h-2 w-2 rounded-full bg-red-500"></span>'
            "Offline</span>"
        )
    if status_value == "degraded":
        return (
            '<span class="inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm font-medium '
            'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400">'
            '<span class="h-2 w-2 rounded-full bg-amber-500"></span>'
            "Degraded</span>"
        )
    return (
        '<span class="inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm font-medium '
        'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400">'
        '<span class="h-2 w-2 rounded-full bg-blue-500"></span>'
        "Maintenance</span>"
    )


def _render_ping_badge(device) -> str:
    if not device.ping_enabled:
        badge_class = "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300"
        label = "Disabled"
    elif device.last_ping_ok:
        badge_class = "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400"
        label = "OK"
    elif device.last_ping_at:
        badge_class = "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400"
        label = "Failed"
    else:
        badge_class = "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300"
        label = "Unknown"
    return (
        f'<span class="inline-flex items-center rounded-full px-3 py-1.5 text-sm font-medium {badge_class}">'
        f"Ping: {label}</span>"
    )


def _render_snmp_badge(device) -> str:
    if not device.snmp_enabled:
        badge_class = "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300"
        label = "Disabled"
    elif device.last_snmp_ok:
        badge_class = "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400"
        label = "OK"
    elif device.last_snmp_at:
        badge_class = "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400"
        label = "Failed"
    else:
        badge_class = "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300"
        label = "Unknown"
    return (
        f'<span class="inline-flex items-center rounded-full px-3 py-1.5 text-sm font-medium {badge_class}">'
        f"SNMP: {label}</span>"
    )


def _render_device_health_content(device_health: dict) -> str:
    last_seen = device_health.get("last_seen")
    last_seen_value = last_seen.strftime("%b %d, %Y %H:%M") if last_seen else "--"
    return (
        '<div class="grid grid-cols-1 gap-4 sm:grid-cols-2">'
        '<div>'
        '<p class="text-xs font-medium uppercase text-slate-500 dark:text-slate-400">CPU</p>'
        f'<p class="mt-1 text-sm font-semibold text-slate-900 dark:text-white">{device_health.get("cpu", "--")}</p>'
        "</div>"
        '<div>'
        '<p class="text-xs font-medium uppercase text-slate-500 dark:text-slate-400">Memory</p>'
        f'<p class="mt-1 text-sm font-semibold text-slate-900 dark:text-white">{device_health.get("memory", "--")}</p>'
        "</div>"
        '<div>'
        '<p class="text-xs font-medium uppercase text-slate-500 dark:text-slate-400">Uptime</p>'
        f'<p class="mt-1 text-sm font-semibold text-slate-900 dark:text-white">{device_health.get("uptime", "--")}</p>'
        "</div>"
        '<div>'
        '<p class="text-xs font-medium uppercase text-slate-500 dark:text-slate-400">Last Seen</p>'
        f'<p class="mt-1 text-sm font-semibold text-slate-900 dark:text-white">{last_seen_value}</p>'
        "</div>"
        '<div>'
        '<p class="text-xs font-medium uppercase text-slate-500 dark:text-slate-400">Rx</p>'
        f'<p class="mt-1 text-sm font-semibold text-slate-900 dark:text-white">{device_health.get("rx", "--")}</p>'
        "</div>"
        '<div>'
        '<p class="text-xs font-medium uppercase text-slate-500 dark:text-slate-400">Tx</p>'
        f'<p class="mt-1 text-sm font-semibold text-slate-900 dark:text-white">{device_health.get("tx", "--")}</p>'
        "</div>"
        "</div>"
    )


@router.post("/core-devices/{device_id}/ping", response_class=HTMLResponse)
def core_device_ping(request: Request, device_id: str, db: Session = Depends(get_db)):
    from app.models.network_monitoring import NetworkDevice, DeviceStatus

    device = db.query(NetworkDevice).filter(NetworkDevice.id == device_id).first()
    if not device:
        return HTMLResponse(
            '<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 '
            'dark:border-red-800 dark:bg-red-900/30 dark:text-red-400">Device not found.</div>',
            status_code=404,
        )

    if not device.mgmt_ip:
        return HTMLResponse(
            '<div class="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700 '
            'dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-400">Management IP is missing.</div>'
        )

    ping_success = False
    try:
        result = subprocess.run(
            _build_ping_command(device.mgmt_ip),
            capture_output=True,
            text=True,
            check=False,
            timeout=4,
        )
        ping_success = result.returncode == 0
    except Exception:
        ping_success = False

    device.last_ping_at = datetime.now(timezone.utc)
    device.last_ping_ok = ping_success
    device.status = DeviceStatus.online if ping_success else DeviceStatus.offline
    db.commit()

    status_label = "reachable" if ping_success else "unreachable"
    message = (
        '<div class="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700 '
        'dark:border-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-400">'
        f"Ping successful: device is {status_label}.</div>"
        if ping_success
        else
        '<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 '
        'dark:border-red-800 dark:bg-red-900/30 dark:text-red-400">'
        f"Ping failed: device is {status_label}.</div>"
    )

    badge = _render_device_status_badge(device.status.value)
    ping_badge = _render_ping_badge(device)
    return HTMLResponse(
        message
        + f'<div id="device-status-badge" hx-swap-oob="true">{badge}</div>'
        + f'<span id="device-ping-badge" hx-swap-oob="true">{ping_badge}</span>'
    )


@router.post("/core-devices/{device_id}/snmp-check", response_class=HTMLResponse)
def core_device_snmp_check(request: Request, device_id: str, db: Session = Depends(get_db)):
    from app.models.network_monitoring import NetworkDevice

    device = db.query(NetworkDevice).filter(NetworkDevice.id == device_id).first()
    if not device:
        return HTMLResponse(
            '<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 '
            'dark:border-red-800 dark:bg-red-900/30 dark:text-red-400">Device not found.</div>',
            status_code=404,
        )

    if not device.snmp_enabled:
        snmp_badge = _render_snmp_badge(device)
        return HTMLResponse(
            f'<span id="device-snmp-badge" hx-swap-oob="true">{snmp_badge}</span>'
        )

    if not device.mgmt_ip and not device.hostname:
        device.last_snmp_at = datetime.now(timezone.utc)
        device.last_snmp_ok = False
        db.commit()
        snmp_badge = _render_snmp_badge(device)
        return HTMLResponse(
            f'<span id="device-snmp-badge" hx-swap-oob="true">{snmp_badge}</span>'
        )

    try:
        from app.services.snmp_discovery import _run_snmpwalk

        _run_snmpwalk(device, ".1.3.6.1.2.1.1.3.0", timeout=8)
        device.last_snmp_at = datetime.now(timezone.utc)
        device.last_snmp_ok = True
        db.commit()
    except Exception:
        device.last_snmp_at = datetime.now(timezone.utc)
        device.last_snmp_ok = False
        db.commit()

    snmp_badge = _render_snmp_badge(device)
    return HTMLResponse(
        f'<span id="device-snmp-badge" hx-swap-oob="true">{snmp_badge}</span>'
    )


@router.post("/core-devices/{device_id}/snmp-debug", response_class=HTMLResponse)
def core_device_snmp_debug(request: Request, device_id: str, db: Session = Depends(get_db)):
    from app.models.network_monitoring import NetworkDevice

    device = db.query(NetworkDevice).filter(NetworkDevice.id == device_id).first()
    if not device:
        return HTMLResponse(
            '<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 '
            'dark:border-red-800 dark:bg-red-900/30 dark:text-red-400">Device not found.</div>',
            status_code=404,
        )

    if not device.snmp_enabled:
        return HTMLResponse(
            '<div class="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700 '
            'dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-400">'
            "SNMP is disabled for this device."
            "</div>"
        )

    if not device.mgmt_ip and not device.hostname:
        return HTMLResponse(
            '<div class="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700 '
            'dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-400">'
            "Management IP or hostname is required for SNMP."
            "</div>"
        )

    try:
        from app.services.snmp_discovery import _run_snmpbulkwalk

        descr_lines = _run_snmpbulkwalk(device, ".1.3.6.1.2.1.2.2.1.2")[:20]
        status_lines = _run_snmpbulkwalk(device, ".1.3.6.1.2.1.2.2.1.8")[:20]
        alias_lines = _run_snmpbulkwalk(device, ".1.3.6.1.2.1.31.1.1.1.18")[:20]
    except Exception as exc:
        return HTMLResponse(
            '<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 '
            'dark:border-red-800 dark:bg-red-900/30 dark:text-red-400">'
            f"SNMP debug failed: {str(exc)}"
            "</div>"
        )

    output = "\n".join(
        [
            "ifDescr:",
            *descr_lines,
            "",
            "ifOperStatus:",
            *status_lines,
            "",
            "ifAlias:",
            *alias_lines,
        ]
    ).strip()
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white p-4 text-xs text-slate-700 '
        'dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200">'
        f"<pre class=\"whitespace-pre-wrap\">{output}</pre>"
        "</div>"
    )


@router.get("/core-devices/{device_id}/health", response_class=HTMLResponse)
def core_device_health_partial(request: Request, device_id: str, db: Session = Depends(get_db)):
    from app.models.network_monitoring import DeviceMetric, MetricType, NetworkDevice

    device = db.query(NetworkDevice).filter(NetworkDevice.id == device_id).first()
    if not device:
        return HTMLResponse(
            '<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 '
            'dark:border-red-800 dark:bg-red-900/30 dark:text-red-400">Device not found.</div>',
            status_code=404,
        )

    selected_interface = None
    selected_interface_id = request.query_params.get("interface_id")
    if selected_interface_id:
        from app.models.network_monitoring import DeviceInterface

        selected_interface = (
            db.query(DeviceInterface)
            .filter(DeviceInterface.id == selected_interface_id, DeviceInterface.device_id == device.id)
            .filter(DeviceInterface.name.ilike("eth%"))
            .first()
        )

    metric_types = [
        MetricType.cpu,
        MetricType.memory,
        MetricType.uptime,
    ]
    if not selected_interface:
        metric_types.extend([MetricType.rx_bps, MetricType.tx_bps])
    latest_metrics_subq = (
        db.query(
            DeviceMetric.metric_type,
            func.max(DeviceMetric.recorded_at).label("latest"),
        )
        .filter(DeviceMetric.device_id == device.id)
        .filter(DeviceMetric.metric_type.in_(metric_types))
        .group_by(DeviceMetric.metric_type)
        .subquery()
    )
    latest_metrics = (
        db.query(DeviceMetric)
        .join(
            latest_metrics_subq,
            and_(
                DeviceMetric.metric_type == latest_metrics_subq.c.metric_type,
                DeviceMetric.recorded_at == latest_metrics_subq.c.latest,
            ),
        )
        .filter(DeviceMetric.device_id == device.id)
        .all()
    )
    metrics_by_type = {metric.metric_type: metric for metric in latest_metrics}
    cpu_metric = metrics_by_type.get(MetricType.cpu)
    mem_metric = metrics_by_type.get(MetricType.memory)
    uptime_metric = metrics_by_type.get(MetricType.uptime)
    rx_metric = None
    tx_metric = None
    if selected_interface:
        interface_metric_types = [MetricType.rx_bps, MetricType.tx_bps]
        interface_metrics_subq = (
            db.query(
                DeviceMetric.metric_type,
                func.max(DeviceMetric.recorded_at).label("latest"),
            )
            .filter(DeviceMetric.device_id == device.id)
            .filter(DeviceMetric.interface_id == selected_interface.id)
            .filter(DeviceMetric.metric_type.in_(interface_metric_types))
            .group_by(DeviceMetric.metric_type)
            .subquery()
        )
        interface_metrics = (
            db.query(DeviceMetric)
            .join(
                interface_metrics_subq,
                and_(
                    DeviceMetric.metric_type == interface_metrics_subq.c.metric_type,
                    DeviceMetric.recorded_at == interface_metrics_subq.c.latest,
                ),
            )
            .filter(DeviceMetric.device_id == device.id)
            .filter(DeviceMetric.interface_id == selected_interface.id)
            .all()
        )
        interface_metrics_by_type = {metric.metric_type: metric for metric in interface_metrics}
        rx_metric = interface_metrics_by_type.get(MetricType.rx_bps)
        tx_metric = interface_metrics_by_type.get(MetricType.tx_bps)
    else:
        rx_metric = metrics_by_type.get(MetricType.rx_bps)
        tx_metric = metrics_by_type.get(MetricType.tx_bps)

    device_health = {
        "cpu": f"{cpu_metric.value:.1f}%" if cpu_metric else "--",
        "memory": f"{mem_metric.value:.1f}%" if mem_metric else "--",
        "uptime": _format_duration(uptime_metric.value if uptime_metric else None),
        "rx": _format_bps(rx_metric.value) if rx_metric else "--",
        "tx": _format_bps(tx_metric.value) if tx_metric else "--",
        "last_seen": device.last_ping_at or device.last_snmp_at,
    }

    return HTMLResponse(
        f'<div id="device-health-content" hx-swap-oob="true">{_render_device_health_content(device_health)}</div>'
    )


@router.post("/core-devices/{device_id}/discover-interfaces", response_class=HTMLResponse)
def core_device_discover_interfaces(request: Request, device_id: str, db: Session = Depends(get_db)):
    from app.models.network_monitoring import DeviceMetric, MetricType, NetworkDevice

    device = db.query(NetworkDevice).filter(NetworkDevice.id == device_id).first()
    if not device:
        return HTMLResponse(
            '<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 '
            'dark:border-red-800 dark:bg-red-900/30 dark:text-red-400">Device not found.</div>',
            status_code=404,
        )

    if not device.snmp_enabled:
        return HTMLResponse(
            '<div class="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700 '
            'dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-400">'
            "SNMP is disabled for this device."
            "</div>"
        )

    if not device.mgmt_ip and not device.hostname:
        return HTMLResponse(
            '<div class="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700 '
            'dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-400">'
            "Management IP or hostname is required for SNMP discovery."
            "</div>"
        )

    try:
        from app.services.snmp_discovery import (
            apply_interface_snapshot,
            collect_device_health,
            collect_interface_snapshot,
        )

        snapshots = collect_interface_snapshot(device)
        created, updated = apply_interface_snapshot(db, device, snapshots, create_missing=True)
        health = collect_device_health(device)
        recorded_at = datetime.now(timezone.utc)
        if health.get("cpu_percent") is not None:
            db.add(DeviceMetric(
                device_id=device.id,
                metric_type=MetricType.cpu,
                value=int(health["cpu_percent"]),
                unit="percent",
                recorded_at=recorded_at,
            ))
        if health.get("memory_percent") is not None:
            db.add(DeviceMetric(
                device_id=device.id,
                metric_type=MetricType.memory,
                value=int(health["memory_percent"]),
                unit="percent",
                recorded_at=recorded_at,
            ))
        if health.get("uptime_seconds") is not None:
            db.add(DeviceMetric(
                device_id=device.id,
                metric_type=MetricType.uptime,
                value=int(health["uptime_seconds"]),
                unit="seconds",
                recorded_at=recorded_at,
            ))
        if health.get("rx_bps") is not None:
            db.add(DeviceMetric(
                device_id=device.id,
                metric_type=MetricType.rx_bps,
                value=int(health["rx_bps"]),
                unit="bps",
                recorded_at=recorded_at,
            ))
        if health.get("tx_bps") is not None:
            db.add(DeviceMetric(
                device_id=device.id,
                metric_type=MetricType.tx_bps,
                value=int(health["tx_bps"]),
                unit="bps",
                recorded_at=recorded_at,
            ))
        device.last_snmp_at = datetime.now(timezone.utc)
        device.last_snmp_ok = True
        db.commit()
    except Exception as exc:
        device.last_snmp_at = datetime.now(timezone.utc)
        device.last_snmp_ok = False
        db.commit()
        return HTMLResponse(
            '<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 '
            'dark:border-red-800 dark:bg-red-900/30 dark:text-red-400">'
            f"SNMP discovery failed: {str(exc)}"
            "</div>"
        )

    refresh = request.query_params.get("refresh", "true").lower() != "false"
    headers = {}
    if refresh:
        headers["HX-Refresh"] = "true"
    else:
        headers["HX-Trigger"] = "snmp-discovered"
    return HTMLResponse(
        '<div class="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700 '
        'dark:border-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-400">'
        f"Discovery complete: {created} new, {updated} updated interfaces."
        "</div>",
        headers=headers,
    )


@router.post("/core-devices/{device_id}", response_class=HTMLResponse)
async def core_device_update(request: Request, device_id: str, db: Session = Depends(get_db)):
    from types import SimpleNamespace
    from app.models.network_monitoring import DeviceRole, DeviceType, NetworkDevice, PopSite
    from sqlalchemy.exc import IntegrityError

    device = db.query(NetworkDevice).filter(NetworkDevice.id == device_id).first()
    if not device:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Device not found"},
            status_code=404,
        )
    before_snapshot = model_to_dict(device)

    form = await request.form()
    name = form.get("name", "").strip()
    hostname = form.get("hostname", "").strip() or None
    mgmt_ip = form.get("mgmt_ip", "").strip() or None
    role_value = form.get("role", "").strip()
    device_type_value = form.get("device_type", "").strip()
    pop_site_id = form.get("pop_site_id", "").strip() or None
    ping_enabled = form.get("ping_enabled") == "true"
    snmp_enabled = form.get("snmp_enabled") == "true"
    role = DeviceRole.edge
    device_type = None
    snmp_port = 161 if snmp_enabled else None

    pop_sites = db.query(PopSite).filter(PopSite.is_active == True).order_by(PopSite.name).all()

    def _error_response(message: str):
        device_snapshot = SimpleNamespace(
            id=device.id,
            name=name,
            hostname=hostname,
            mgmt_ip=mgmt_ip,
            role=role,
            pop_site_id=pop_site_id,
            vendor=form.get("vendor", "").strip() or None,
            model=form.get("model", "").strip() or None,
            serial_number=form.get("serial_number", "").strip() or None,
            device_type=device_type,
            ping_enabled=ping_enabled,
            snmp_enabled=snmp_enabled,
            snmp_port=snmp_port,
            snmp_version=form.get("snmp_version", "").strip() or None,
            snmp_community=form.get("snmp_community", "").strip() or None,
            snmp_username=form.get("snmp_username", "").strip() or None,
            snmp_auth_protocol=form.get("snmp_auth_protocol", "").strip() or None,
            snmp_auth_secret=form.get("snmp_auth_secret", "").strip() or None,
            snmp_priv_protocol=form.get("snmp_priv_protocol", "").strip() or None,
            snmp_priv_secret=form.get("snmp_priv_secret", "").strip() or None,
            notes=form.get("notes", "").strip() or None,
            is_active=form.get("is_active") == "true",
        )
        context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
        context.update({
            "device": device_snapshot,
            "pop_sites": pop_sites,
            "action_url": f"/admin/network/core-devices/{device.id}",
            "error": message,
        })
        return templates.TemplateResponse("admin/network/core-devices/form.html", context)

    if not name:
        return _error_response("Device name is required")

    try:
        role = DeviceRole(role_value)
    except ValueError:
        role = device.role
        return _error_response("Invalid device role")

    try:
        device_type = DeviceType(device_type_value) if device_type_value else None
    except ValueError:
        device_type = device.device_type
        return _error_response("Invalid device type")

    snmp_port_value = form.get("snmp_port", "").strip()
    if snmp_port_value:
        try:
            snmp_port = int(snmp_port_value)
        except ValueError:
            snmp_port = device.snmp_port or 161
            return _error_response("SNMP port must be a valid number")
    else:
        snmp_port = 161 if snmp_enabled else None

    if pop_site_id:
        pop_site = db.query(PopSite).filter(PopSite.id == pop_site_id).first()
        if not pop_site:
            return _error_response("Selected POP site was not found")

    if hostname:
        existing = (
            db.query(NetworkDevice)
            .filter(NetworkDevice.hostname == hostname)
            .filter(NetworkDevice.id != device.id)
            .first()
        )
        if existing:
            return _error_response("Hostname already exists")
    if mgmt_ip:
        existing = (
            db.query(NetworkDevice)
            .filter(NetworkDevice.mgmt_ip == mgmt_ip)
            .filter(NetworkDevice.id != device.id)
            .first()
        )
        if existing:
            return _error_response("Management IP already exists")

    host = mgmt_ip or hostname
    if ping_enabled and not host:
        return _error_response("Management IP or hostname is required for ping checks.")
    if snmp_enabled and not host:
        return _error_response("Management IP or hostname is required for SNMP checks.")

    if ping_enabled and host:
        try:
            result = subprocess.run(
                _build_ping_command(host),
                capture_output=True,
                text=True,
                check=False,
                timeout=4,
            )
            if result.returncode != 0:
                return _error_response("Ping failed. Check the management IP/hostname.")
        except Exception:
            return _error_response("Ping failed. Check the management IP/hostname.")
        device.last_ping_at = datetime.now(timezone.utc)
        device.last_ping_ok = True

    if snmp_enabled:
        try:
            from app.services.snmp_discovery import _run_snmpwalk

            probe = SimpleNamespace(
                hostname=hostname,
                mgmt_ip=mgmt_ip,
                snmp_version=form.get("snmp_version", "").strip() or None,
                snmp_community=form.get("snmp_community", "").strip() or None,
                snmp_username=form.get("snmp_username", "").strip() or None,
                snmp_auth_protocol=form.get("snmp_auth_protocol", "").strip() or None,
                snmp_auth_secret=form.get("snmp_auth_secret", "").strip() or None,
                snmp_priv_protocol=form.get("snmp_priv_protocol", "").strip() or None,
                snmp_priv_secret=form.get("snmp_priv_secret", "").strip() or None,
                snmp_port=snmp_port,
            )
            _run_snmpwalk(probe, ".1.3.6.1.2.1.1.3.0", timeout=8)
            device.last_snmp_at = datetime.now(timezone.utc)
            device.last_snmp_ok = True
        except Exception as exc:
            return _error_response(f"SNMP check failed: {str(exc)}")

    device.name = name
    device.hostname = hostname
    device.mgmt_ip = mgmt_ip
    device.role = role
    device.pop_site_id = pop_site_id
    device.vendor = form.get("vendor", "").strip() or None
    device.model = form.get("model", "").strip() or None
    device.serial_number = form.get("serial_number", "").strip() or None
    device.device_type = device_type
    device.ping_enabled = ping_enabled
    device.snmp_enabled = snmp_enabled
    device.snmp_port = snmp_port
    device.snmp_version = form.get("snmp_version", "").strip() or None
    device.snmp_community = form.get("snmp_community", "").strip() or None
    device.snmp_username = form.get("snmp_username", "").strip() or None
    device.snmp_auth_protocol = form.get("snmp_auth_protocol", "").strip() or None
    device.snmp_auth_secret = form.get("snmp_auth_secret", "").strip() or None
    device.snmp_priv_protocol = form.get("snmp_priv_protocol", "").strip() or None
    device.snmp_priv_secret = form.get("snmp_priv_secret", "").strip() or None
    device.notes = form.get("notes", "").strip() or None
    device.is_active = form.get("is_active") == "true"

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        error = _core_device_integrity_error_message(exc)
        device_snapshot = SimpleNamespace(
            name=name,
            hostname=device.hostname,
            mgmt_ip=device.mgmt_ip,
            role=role,
            pop_site_id=pop_site_id,
            vendor=device.vendor,
            model=device.model,
            serial_number=device.serial_number,
            device_type=device_type,
            ping_enabled=ping_enabled,
            snmp_enabled=snmp_enabled,
            snmp_port=snmp_port,
            snmp_version=device.snmp_version,
            snmp_community=device.snmp_community,
            snmp_username=device.snmp_username,
            snmp_auth_protocol=device.snmp_auth_protocol,
            snmp_auth_secret=device.snmp_auth_secret,
            snmp_priv_protocol=device.snmp_priv_protocol,
            snmp_priv_secret=device.snmp_priv_secret,
            notes=device.notes,
            is_active=device.is_active,
        )
        context = _base_context(request, db, active_page="core-devices", active_menu="core-network")
        context.update({
            "device": device_snapshot,
            "pop_sites": pop_sites,
            "action_url": f"/admin/network/core-devices/{device.id}",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/core-devices/form.html", context)

    after_snapshot = model_to_dict(device)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata_payload = {"changes": changes} if changes else None
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="core_device",
        entity_id=str(device.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata_payload,
    )
    return RedirectResponse(f"/admin/network/core-devices/{device.id}", status_code=303)


# ==================== Fiber Plant (ODN) ====================

@router.get("/fiber-plant", response_class=HTMLResponse)
def fiber_plant_consolidated(
    request: Request,
    tab: str = "cabinets",
    db: Session = Depends(get_db),
):
    """Consolidated view of fiber plant infrastructure."""
    from app.models.network import FdhCabinet, Splitter, FiberStrand, FiberSpliceClosure
    from app.models.fiber_change_request import FiberChangeRequest, FiberChangeRequestStatus

    # FDH Cabinets
    cabinets = db.query(FdhCabinet).filter(FdhCabinet.is_active == True).order_by(FdhCabinet.name).limit(200).all()

    # Splitters
    splitters = db.query(Splitter).filter(Splitter.is_active == True).order_by(Splitter.name).limit(200).all()

    # Fiber Strands
    strands = db.query(FiberStrand).order_by(FiberStrand.cable_name, FiberStrand.strand_number).limit(200).all()
    strands_available = sum(1 for s in strands if s.status.value == "available")
    strands_in_use = sum(1 for s in strands if s.status.value == "in_use")

    # Splice Closures
    closures = db.query(FiberSpliceClosure).filter(FiberSpliceClosure.is_active == True).order_by(FiberSpliceClosure.name).limit(200).all()

    # Change Requests (pending)
    change_requests = change_request_service.list_requests(
        db, status=FiberChangeRequestStatus.pending
    )

    stats = {
        "cabinets": len(cabinets),
        "splitters": len(splitters),
        "strands": len(strands),
        "strands_available": strands_available,
        "strands_in_use": strands_in_use,
        "closures": len(closures),
        "pending_changes": len(change_requests),
    }

    context = _base_context(request, db, active_page="fiber-plant", active_menu="fiber")
    context.update({
        "tab": tab,
        "stats": stats,
        "cabinets": cabinets,
        "splitters": splitters,
        "strands": strands,
        "closures": closures,
        "change_requests": change_requests,
    })
    return templates.TemplateResponse("admin/network/fiber-plant/index.html", context)


@router.get("/fiber-map", response_class=HTMLResponse)
def fiber_plant_map(request: Request, db: Session = Depends(get_db)):
    """Interactive fiber plant map."""
    import json
    from sqlalchemy import func
    from app.models.network import FdhCabinet, FiberSpliceClosure, FiberSegment, Splitter, FiberSplice, FiberSpliceTray, FiberAccessPoint
    from app.services import settings_spec
    from app.models.domain_settings import SettingDomain

    features = []

    # FDH Cabinets
    fdh_cabinets = db.query(FdhCabinet).filter(
        FdhCabinet.is_active.is_(True),
        FdhCabinet.latitude.isnot(None),
        FdhCabinet.longitude.isnot(None)
    ).all()
    splitter_counts = {}
    if fdh_cabinets:
        fdh_ids = [fdh.id for fdh in fdh_cabinets]
        splitter_counts = dict(
            db.query(Splitter.fdh_id, func.count(Splitter.id))
            .filter(Splitter.fdh_id.in_(fdh_ids))
            .group_by(Splitter.fdh_id)
            .all()
        )
    for fdh in fdh_cabinets:
        splitter_count = splitter_counts.get(fdh.id, 0)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [fdh.longitude, fdh.latitude]},
            "properties": {
                "id": str(fdh.id),
                "type": "fdh_cabinet",
                "name": fdh.name,
                "code": fdh.code,
                "splitter_count": splitter_count,
            },
        })

    # Splice Closures
    closures = db.query(FiberSpliceClosure).filter(
        FiberSpliceClosure.is_active.is_(True),
        FiberSpliceClosure.latitude.isnot(None),
        FiberSpliceClosure.longitude.isnot(None)
    ).all()
    splice_counts = {}
    tray_counts = {}
    if closures:
        closure_ids = [closure.id for closure in closures]
        splice_counts = dict(
            db.query(FiberSplice.closure_id, func.count(FiberSplice.id))
            .filter(FiberSplice.closure_id.in_(closure_ids))
            .group_by(FiberSplice.closure_id)
            .all()
        )
        tray_counts = dict(
            db.query(FiberSpliceTray.closure_id, func.count(FiberSpliceTray.id))
            .filter(FiberSpliceTray.closure_id.in_(closure_ids))
            .group_by(FiberSpliceTray.closure_id)
            .all()
        )
    for closure in closures:
        splice_count = splice_counts.get(closure.id, 0)
        tray_count = tray_counts.get(closure.id, 0)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [closure.longitude, closure.latitude]},
            "properties": {
                "id": str(closure.id),
                "type": "splice_closure",
                "name": closure.name,
                "splice_count": splice_count,
                "tray_count": tray_count,
            },
        })

    # Fiber Access Points
    access_points = db.query(FiberAccessPoint).filter(
        FiberAccessPoint.is_active.is_(True),
        FiberAccessPoint.latitude.isnot(None),
        FiberAccessPoint.longitude.isnot(None)
    ).all()
    for ap in access_points:
        features.append({
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
        })

    # Fiber Segments
    segments = db.query(FiberSegment).filter(FiberSegment.is_active.is_(True)).all()
    segment_geoms = db.query(FiberSegment, func.ST_AsGeoJSON(FiberSegment.route_geom)).filter(
        FiberSegment.is_active.is_(True),
        FiberSegment.route_geom.isnot(None),
    ).all()
    for segment, geojson_str in segment_geoms:
        if not geojson_str:
            continue
        geom = json.loads(geojson_str)
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "id": str(segment.id),
                "type": "fiber_segment",
                "name": segment.name,
                "segment_type": segment.segment_type.value if segment.segment_type else None,
                "cable_type": segment.cable_type.value if segment.cable_type else None,
                "fiber_count": segment.fiber_count,
                "length_m": segment.length_m,
            },
        })

    geojson_data = {"type": "FeatureCollection", "features": features}

    # Stats
    stats = {
        "fdh_cabinets": db.query(func.count(FdhCabinet.id)).filter(FdhCabinet.is_active.is_(True)).scalar(),
        "fdh_with_location": len(fdh_cabinets),
        "splice_closures": db.query(func.count(FiberSpliceClosure.id)).filter(FiberSpliceClosure.is_active.is_(True)).scalar(),
        "closures_with_location": len(closures),
        "splitters": db.query(func.count(Splitter.id)).filter(Splitter.is_active.is_(True)).scalar(),
        "total_splices": db.query(func.count(FiberSplice.id)).scalar(),
        "segments": len(segments),
        "access_points": db.query(func.count(FiberAccessPoint.id)).filter(FiberAccessPoint.is_active.is_(True)).scalar(),
        "access_points_with_location": len(access_points),
    }

    # Fiber installation cost settings
    cost_settings = {
        "drop_cable_per_meter": float(settings_spec.resolve_value(db, SettingDomain.network, "fiber_drop_cable_cost_per_meter") or "2.50"),
        "labor_per_meter": float(settings_spec.resolve_value(db, SettingDomain.network, "fiber_labor_cost_per_meter") or "1.50"),
        "ont_device": float(settings_spec.resolve_value(db, SettingDomain.network, "fiber_ont_device_cost") or "85.00"),
        "installation_base": float(settings_spec.resolve_value(db, SettingDomain.network, "fiber_installation_base_fee") or "50.00"),
        "currency": settings_spec.resolve_value(db, SettingDomain.billing, "default_currency") or "NGN",
    }

    context = _base_context(request, db, active_page="fiber-map", active_menu="fiber")
    context.update({
        "geojson_data": geojson_data,
        "stats": stats,
        "cost_settings": cost_settings,
    })
    return templates.TemplateResponse("admin/network/fiber/map.html", context)


@router.get("/fiber-change-requests", response_class=HTMLResponse)
def fiber_change_requests(request: Request, db: Session = Depends(get_db)):
    """Review pending vendor fiber change requests."""
    from app.models.fiber_change_request import FiberChangeRequestStatus

    requests = change_request_service.list_requests(
        db, status=FiberChangeRequestStatus.pending
    )
    conflicts = {str(req.id): _has_conflict(db, req) for req in requests}
    bulk_status = request.query_params.get("bulk")
    skipped = request.query_params.get("skipped")
    context = _base_context(request, db, active_page="fiber-change-requests", active_menu="fiber")
    context.update(
        {
            "requests": requests,
            "conflicts": conflicts,
            "bulk_status": bulk_status,
            "skipped": skipped,
        }
    )
    return templates.TemplateResponse(
        "admin/network/fiber/change_requests.html", context
    )


@router.get("/fiber-change-requests/{request_id}", response_class=HTMLResponse)
def fiber_change_request_detail(request: Request, request_id: str, db: Session = Depends(get_db)):
    """Review a specific fiber change request."""
    from app.models.fiber_change_request import FiberChangeRequestStatus
    from app.services import fiber_change_requests as change_requests

    change_request = change_requests.get_request(db, request_id)
    asset = None
    asset_data: dict[str, object] = {}
    conflict = _has_conflict(db, change_request)
    if change_request.asset_id:
        asset_type, model = change_requests._get_model(change_request.asset_type)
        asset = db.get(model, change_request.asset_id)
        asset_data = _serialize_asset(asset)

    context = _base_context(request, db, active_page="fiber-change-requests", active_menu="fiber")
    context.update(
        {
            "change_request": change_request,
            "asset_data": asset_data,
            "conflict": conflict,
            "pending": change_request.status == FiberChangeRequestStatus.pending,
            "error": request.query_params.get("error"),
            "activities": _build_audit_activities(
                db,
                "fiber_change_request",
                str(request_id),
                limit=10,
            ),
        }
    )
    return templates.TemplateResponse(
        "admin/network/fiber/change_request_detail.html", context
    )


@router.post("/fiber-change-requests/{request_id}/approve")
async def fiber_change_request_approve(request: Request, request_id: str, db: Session = Depends(get_db)):
    data = await request.form()
    review_notes = data.get("review_notes")
    force_apply = data.get("force_apply") == "true"
    current_user = _base_context(request, db, active_page="fiber-change-requests")["current_user"]
    change_request = change_request_service.get_request(db, request_id)
    if _has_conflict(db, change_request) and not force_apply:
        return RedirectResponse(
            url=f"/admin/network/fiber-change-requests/{request_id}?error=conflict",
            status_code=303,
        )
    change_request_service.approve_request(
        db, request_id, reviewer_person_id=current_user["person_id"], review_notes=review_notes
    )
    log_audit_event(
        db=db,
        request=request,
        action="approve",
        entity_type="fiber_change_request",
        entity_id=str(request_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"force_apply": force_apply, "review_notes": review_notes},
    )
    return RedirectResponse(
        url=f"/admin/network/fiber-change-requests/{request_id}", status_code=303
    )


@router.post("/fiber-change-requests/{request_id}/reject")
async def fiber_change_request_reject(request: Request, request_id: str, db: Session = Depends(get_db)):
    data = await request.form()
    review_notes = data.get("review_notes")
    if not review_notes or not str(review_notes).strip():
        return RedirectResponse(
            url=f"/admin/network/fiber-change-requests/{request_id}?error=reject_note_required",
            status_code=303,
        )
    current_user = _base_context(request, db, active_page="fiber-change-requests")["current_user"]
    change_request_service.reject_request(
        db, request_id, reviewer_person_id=current_user["person_id"], review_notes=review_notes
    )
    log_audit_event(
        db=db,
        request=request,
        action="reject",
        entity_type="fiber_change_request",
        entity_id=str(request_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"review_notes": review_notes},
    )
    return RedirectResponse(
        url=f"/admin/network/fiber-change-requests/{request_id}", status_code=303
    )


@router.post("/fiber-change-requests/bulk-approve")
async def fiber_change_requests_bulk_approve(request: Request, db: Session = Depends(get_db)):
    data = await request.form()
    request_ids = data.getlist("request_ids")
    force_apply = data.get("force_apply") == "true"
    current_user = _base_context(request, db, active_page="fiber-change-requests")["current_user"]
    skipped = 0
    for request_id in request_ids:
        change_request = change_request_service.get_request(db, request_id)
        if _has_conflict(db, change_request) and not force_apply:
            skipped += 1
            continue
        change_request_service.approve_request(
            db,
            request_id,
            reviewer_person_id=current_user["person_id"],
            review_notes="Bulk approved",
        )
        log_audit_event(
            db=db,
            request=request,
            action="approve",
            entity_type="fiber_change_request",
            entity_id=str(request_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"force_apply": force_apply, "review_notes": "Bulk approved"},
        )
    return RedirectResponse(
        url=f"/admin/network/fiber-change-requests?bulk=approved&skipped={skipped}",
        status_code=303,
    )


@router.post("/fiber-map/save-plan")
async def fiber_map_save_plan(request: Request, db: Session = Depends(get_db)):
    """Persist a planned route to a project quote."""
    data = await request.json()
    quote_id = data.get("quote_id")
    geojson = data.get("geojson")
    length_meters = data.get("length_meters")
    if not quote_id or not geojson:
        return JSONResponse({"error": "quote_id and geojson are required"}, status_code=400)
    revision = vendor_service.proposed_route_revisions.create_for_admin(
        db, quote_id=quote_id, geojson=geojson, length_meters=length_meters
    )
    return JSONResponse(
        {
            "success": True,
            "revision_id": str(revision.id),
            "revision_number": revision.revision_number,
        }
    )


@router.post("/fiber-map/update-position")
async def update_asset_position(request: Request, db: Session = Depends(get_db)):
    """Update position of FDH cabinet or splice closure via drag-and-drop."""
    from app.models.network import FdhCabinet, FiberSpliceClosure
    from fastapi.responses import JSONResponse

    try:
        data = await request.json()
        asset_type = data.get("type")
        asset_id = data.get("id")
        latitude = data.get("latitude")
        longitude = data.get("longitude")

        if not all([asset_type, asset_id, latitude is not None, longitude is not None]):
            return JSONResponse({"error": "Missing required fields"}, status_code=400)

        try:
            latitude = float(latitude)
            longitude = float(longitude)
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid coordinates"}, status_code=400)

        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return JSONResponse({"error": "Coordinates out of range"}, status_code=400)

        if asset_type == "fdh_cabinet":
            asset = db.query(FdhCabinet).filter(FdhCabinet.id == asset_id).first()
        elif asset_type == "splice_closure":
            asset = db.query(FiberSpliceClosure).filter(FiberSpliceClosure.id == asset_id).first()
        else:
            return JSONResponse({"error": "Invalid asset type"}, status_code=400)

        if not asset:
            return JSONResponse({"error": "Asset not found"}, status_code=404)

        asset.latitude = latitude
        asset.longitude = longitude
        db.commit()

        return JSONResponse({"success": True, "id": str(asset.id), "latitude": latitude, "longitude": longitude})

    except Exception as e:
        db.rollback()
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/fiber-map/nearest-cabinet")
async def find_nearest_cabinet(request: Request, lat: float, lng: float, db: Session = Depends(get_db)):
    """Find nearest FDH cabinet to given coordinates for installation planning."""
    from fastapi.responses import JSONResponse
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    max_km = settings_spec.resolve_value(db, SettingDomain.gis, "map_nearest_search_max_km") or 50
    snap_max_m = settings_spec.resolve_value(db, SettingDomain.gis, "map_snap_max_m") or 250
    allow_fallback = settings_spec.resolve_value(
        db, SettingDomain.gis, "map_allow_straightline_fallback"
    )

    cabinets = _nearby_cabinets(db, lat, lng, max_km)
    if not cabinets:
        return JSONResponse({"error": f"No FDH cabinets found within {max_km} km"}, status_code=404)

    nearest = None
    min_distance = float("inf")
    for cabinet in cabinets:
        distance = _haversine_distance(lat, lng, cabinet.latitude, cabinet.longitude)
        if distance < min_distance:
            min_distance = distance
            nearest = cabinet

    if not nearest:
        return JSONResponse({"error": "Could not calculate nearest cabinet"}, status_code=500)

    path_coords = None
    path_type = "straight"
    graph, edges = _build_fiber_graph(db)
    start_node, _ = _snap_to_graph(lat, lng, graph, edges, snap_max_m)
    cabinet_node, _ = _snap_to_graph(nearest.latitude, nearest.longitude, graph, edges, snap_max_m)
    if start_node and cabinet_node:
        distance, path = _shortest_path(graph, start_node, cabinet_node)
        if distance is not None and path:
            min_distance = distance
            path_coords = [[node[1], node[0]] for node in path]
            path_type = "fiber"
    if path_coords is None and not allow_fallback:
        return JSONResponse({"error": "No fiber route found to nearest cabinet"}, status_code=404)

    # Format distance for display
    if min_distance >= 1000:
        distance_display = f"{min_distance / 1000:.2f} km"
    else:
        distance_display = f"{min_distance:.0f} m"

    return JSONResponse({
        "cabinet": {
            "id": str(nearest.id),
            "name": nearest.name,
            "code": nearest.code,
            "latitude": nearest.latitude,
            "longitude": nearest.longitude,
        },
        "distance_m": round(min_distance, 2),
        "distance_display": distance_display,
        "path_coords": path_coords,
        "path_type": path_type,
        "customer_coords": {"latitude": lat, "longitude": lng},
    })


@router.get("/fiber-map/plan-options")
async def plan_options(request: Request, lat: float, lng: float, db: Session = Depends(get_db)):
    """List nearby cabinets for planning and manual routing."""
    from fastapi.responses import JSONResponse
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    max_km = settings_spec.resolve_value(db, SettingDomain.gis, "map_nearest_search_max_km") or 50
    cabinets = _nearby_cabinets(db, lat, lng, max_km)
    if not cabinets:
        return JSONResponse({"error": f"No FDH cabinets found within {max_km} km"}, status_code=404)

    options = []
    for cabinet in cabinets:
        distance = _haversine_distance(lat, lng, cabinet.latitude, cabinet.longitude)
        if distance >= 1000:
            distance_display = f"{distance / 1000:.2f} km"
        else:
            distance_display = f"{distance:.0f} m"
        options.append({
            "id": str(cabinet.id),
            "name": cabinet.name,
            "code": cabinet.code,
            "latitude": cabinet.latitude,
            "longitude": cabinet.longitude,
            "distance_m": round(distance, 2),
            "distance_display": distance_display,
        })

    options.sort(key=lambda item: item["distance_m"])
    options = options[:10]

    return JSONResponse({
        "options": options,
        "customer_coords": {"latitude": lat, "longitude": lng},
    })


@router.get("/fiber-map/route")
async def plan_route(request: Request, lat: float, lng: float, cabinet_id: str, db: Session = Depends(get_db)):
    """Calculate a fiber route between a point and a cabinet."""
    from fastapi.responses import JSONResponse
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec
    from app.models.network import FdhCabinet

    cabinet = db.query(FdhCabinet).filter(
        FdhCabinet.id == cabinet_id,
        FdhCabinet.is_active.is_(True),
        FdhCabinet.latitude.isnot(None),
        FdhCabinet.longitude.isnot(None),
    ).first()
    if not cabinet:
        return JSONResponse({"error": "Cabinet not found"}, status_code=404)

    snap_max_m = settings_spec.resolve_value(db, SettingDomain.gis, "map_snap_max_m") or 250
    graph, edges = _build_fiber_graph(db)
    start_node, start_snap = _snap_to_graph(lat, lng, graph, edges, snap_max_m)
    cabinet_node, cabinet_snap = _snap_to_graph(cabinet.latitude, cabinet.longitude, graph, edges, snap_max_m)
    if not start_node or not cabinet_node:
        return JSONResponse(
            {"error": "Unable to snap to fiber network", "start_snap_m": start_snap, "cabinet_snap_m": cabinet_snap},
            status_code=404,
        )

    distance, path = _shortest_path(graph, start_node, cabinet_node)
    if distance is None or not path:
        return JSONResponse({"error": "No fiber route found"}, status_code=404)

    if distance >= 1000:
        distance_display = f"{distance / 1000:.2f} km"
    else:
        distance_display = f"{distance:.0f} m"

    return JSONResponse({
        "cabinet": {
            "id": str(cabinet.id),
            "name": cabinet.name,
            "code": cabinet.code,
            "latitude": cabinet.latitude,
            "longitude": cabinet.longitude,
        },
        "distance_m": round(distance, 2),
        "distance_display": distance_display,
        "path_coords": [[node[1], node[0]] for node in path],
        "path_type": "fiber",
    })


@router.get("/fiber-reports", response_class=HTMLResponse)
def fiber_reports(request: Request, db: Session = Depends(get_db), map_limit: int | None = None):
    """Fiber network deployment reports with asset statistics and customer map."""
    from sqlalchemy import func
    from app.models.network import (
        FdhCabinet, FiberSpliceClosure, FiberSegment, Splitter,
        FiberSplice, FiberSpliceTray, OntAssignment, OntUnit
    )
    from app.models.subscriber import Address, Subscriber, SubscriberAccount

    # Asset Statistics
    stats = {
        "fdh_cabinets": {
            "total": db.query(func.count(FdhCabinet.id)).scalar() or 0,
            "active": db.query(func.count(FdhCabinet.id)).filter(FdhCabinet.is_active.is_(True)).scalar() or 0,
            "with_location": db.query(func.count(FdhCabinet.id)).filter(
                FdhCabinet.latitude.isnot(None), FdhCabinet.longitude.isnot(None)
            ).scalar() or 0,
        },
        "splice_closures": {
            "total": db.query(func.count(FiberSpliceClosure.id)).scalar() or 0,
            "active": db.query(func.count(FiberSpliceClosure.id)).filter(FiberSpliceClosure.is_active.is_(True)).scalar() or 0,
            "with_location": db.query(func.count(FiberSpliceClosure.id)).filter(
                FiberSpliceClosure.latitude.isnot(None), FiberSpliceClosure.longitude.isnot(None)
            ).scalar() or 0,
        },
        "splitters": {
            "total": db.query(func.count(Splitter.id)).scalar() or 0,
            "active": db.query(func.count(Splitter.id)).filter(Splitter.is_active.is_(True)).scalar() or 0,
        },
        "splices": {
            "total": db.query(func.count(FiberSplice.id)).scalar() or 0,
        },
        "trays": {
            "total": db.query(func.count(FiberSpliceTray.id)).scalar() or 0,
        },
        "ont_units": {
            "total": db.query(func.count(OntUnit.id)).scalar() or 0,
            "active": db.query(func.count(OntUnit.id)).filter(OntUnit.is_active.is_(True)).scalar() or 0,
            "assigned": db.query(func.count(OntAssignment.id)).filter(OntAssignment.active.is_(True)).scalar() or 0,
        },
    }

    # Fiber Segments by type
    segments = db.query(FiberSegment).filter(FiberSegment.is_active.is_(True)).all()
    segment_stats = {"feeder": {"count": 0, "length": 0}, "distribution": {"count": 0, "length": 0}, "drop": {"count": 0, "length": 0}}
    for seg in segments:
        seg_type = seg.segment_type.value if seg.segment_type else "distribution"
        if seg_type in segment_stats:
            segment_stats[seg_type]["count"] += 1
            segment_stats[seg_type]["length"] += seg.length_m or 0
    # Calculate totals before adding to stats
    total_length = sum(s["length"] for s in segment_stats.values())
    stats["segments"] = segment_stats
    stats["segments"]["total_count"] = len(segments)
    stats["segments"]["total_length"] = total_length

    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    if map_limit is None:
        map_limit = settings_spec.resolve_value(db, SettingDomain.gis, "map_customer_limit")
    if map_limit is not None and map_limit <= 0:
        map_limit = None

    # Customer locations with fiber service
    customer_features = []
    from app.models.subscriber import Subscriber
    # Get addresses with coordinates that have ONT assignments, join to get person name
    customer_total = db.query(func.count(Address.id)).join(
        OntAssignment, OntAssignment.service_address_id == Address.id
    ).join(
        Subscriber, Address.subscriber_id == Subscriber.id
    ).filter(
        OntAssignment.active.is_(True),
        Address.latitude.isnot(None),
        Address.longitude.isnot(None)
    ).scalar() or 0

    customer_addresses_query = db.query(
        Address.id,
        Address.address_line1,
        Address.city,
        Address.latitude,
        Address.longitude,
        Subscriber.first_name,
        Subscriber.last_name
    ).join(
        OntAssignment, OntAssignment.service_address_id == Address.id
    ).join(
        Subscriber, Address.subscriber_id == Subscriber.id
    ).filter(
        OntAssignment.active.is_(True),
        Address.latitude.isnot(None),
        Address.longitude.isnot(None)
    ).order_by(Address.id)
    if map_limit:
        customer_addresses_query = customer_addresses_query.limit(map_limit)
    customer_addresses = customer_addresses_query.all()

    for addr in customer_addresses:
        subscriber_name = f"{addr.first_name or ''} {addr.last_name or ''}".strip() or "Unknown"

        customer_features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [addr.longitude, addr.latitude]},
            "properties": {
                "id": str(addr.id),
                "type": "customer",
                "name": subscriber_name,
                "address": addr.address_line1,
                "city": addr.city or "",
            }
        })

    # Also include FDH cabinets and closures for context
    fdh_cabinets = db.query(FdhCabinet).filter(
        FdhCabinet.is_active.is_(True),
        FdhCabinet.latitude.isnot(None),
        FdhCabinet.longitude.isnot(None)
    ).all()
    for fdh in fdh_cabinets:
        customer_features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [fdh.longitude, fdh.latitude]},
            "properties": {
                "id": str(fdh.id),
                "type": "fdh_cabinet",
                "name": fdh.name,
                "code": fdh.code,
            }
        })

    closures = db.query(FiberSpliceClosure).filter(
        FiberSpliceClosure.is_active.is_(True),
        FiberSpliceClosure.latitude.isnot(None),
        FiberSpliceClosure.longitude.isnot(None)
    ).all()
    for closure in closures:
        customer_features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [closure.longitude, closure.latitude]},
            "properties": {
                "id": str(closure.id),
                "type": "splice_closure",
                "name": closure.name,
            }
        })

    customer_geojson = {"type": "FeatureCollection", "features": customer_features}

    context = _base_context(request, db, active_page="fiber-reports", active_menu="fiber")
    context.update({
        "stats": stats,
        "customer_geojson": customer_geojson,
        "customer_count": customer_total,
        "customer_map_count": len(customer_addresses),
    })
    return templates.TemplateResponse("admin/network/fiber/reports.html", context)


@router.get("/fdh-cabinets", response_class=HTMLResponse)
def fdh_cabinets_list(request: Request, db: Session = Depends(get_db)):
    """List FDH cabinets."""
    from app.models.network import FdhCabinet

    cabinets = db.query(FdhCabinet).filter(FdhCabinet.is_active == True).order_by(FdhCabinet.name).limit(200).all()

    stats = {"total": len(cabinets)}

    context = _base_context(request, db, active_page="fdh-cabinets", active_menu="fiber")
    context.update({"cabinets": cabinets, "stats": stats})
    return templates.TemplateResponse("admin/network/fiber/fdh-cabinets.html", context)


@router.get("/fdh-cabinets/new", response_class=HTMLResponse)
def fdh_cabinet_new(request: Request, db: Session = Depends(get_db)):
    from app.services import catalog as catalog_service
    regions = catalog_service.region_zones.list(
        db=db, is_active=True, order_by="name", order_dir="asc", limit=500, offset=0
    )

    context = _base_context(request, db, active_page="fdh-cabinets", active_menu="fiber")
    context.update({
        "cabinet": None,
        "regions": regions,
        "action_url": "/admin/network/fdh-cabinets",
    })
    return templates.TemplateResponse("admin/network/fiber/fdh-cabinet-form.html", context)


@router.post("/fdh-cabinets", response_class=HTMLResponse)
async def fdh_cabinet_create(request: Request, db: Session = Depends(get_db)):
    from app.models.network import FdhCabinet
    from app.services import catalog as catalog_service

    form = await request.form()
    name = form.get("name", "").strip()
    code = form.get("code", "").strip() or None
    region_id = form.get("region_id", "").strip() or None
    latitude = form.get("latitude", "").strip()
    longitude = form.get("longitude", "").strip()
    notes = form.get("notes", "").strip() or None
    is_active = form.get("is_active") == "true"

    if not name:
        regions = catalog_service.region_zones.list(
            db=db, is_active=True, order_by="name", order_dir="asc", limit=500, offset=0
        )
        context = _base_context(request, db, active_page="fdh-cabinets", active_menu="fiber")
        context.update({
            "cabinet": None,
            "regions": regions,
            "action_url": "/admin/network/fdh-cabinets",
            "error": "Cabinet name is required",
        })
        return templates.TemplateResponse("admin/network/fiber/fdh-cabinet-form.html", context)

    cabinet = FdhCabinet(
        name=name,
        code=code,
        region_id=region_id,
        latitude=float(latitude) if latitude else None,
        longitude=float(longitude) if longitude else None,
        notes=notes,
        is_active=is_active,
    )
    db.add(cabinet)
    db.commit()
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="fdh_cabinet",
        entity_id=str(cabinet.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": cabinet.name, "code": cabinet.code},
    )

    return RedirectResponse(f"/admin/network/fdh-cabinets/{cabinet.id}", status_code=303)


@router.get("/fdh-cabinets/{cabinet_id}/edit", response_class=HTMLResponse)
def fdh_cabinet_edit(request: Request, cabinet_id: str, db: Session = Depends(get_db)):
    from app.models.network import FdhCabinet
    from app.services import catalog as catalog_service

    cabinet = db.query(FdhCabinet).filter(FdhCabinet.id == cabinet_id).first()
    if not cabinet:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "FDH Cabinet not found"},
            status_code=404,
        )

    regions = catalog_service.region_zones.list(
        db=db, is_active=True, order_by="name", order_dir="asc", limit=500, offset=0
    )

    context = _base_context(request, db, active_page="fdh-cabinets", active_menu="fiber")
    context.update({
        "cabinet": cabinet,
        "regions": regions,
        "action_url": f"/admin/network/fdh-cabinets/{cabinet.id}",
    })
    return templates.TemplateResponse("admin/network/fiber/fdh-cabinet-form.html", context)


@router.post("/fdh-cabinets/{cabinet_id}", response_class=HTMLResponse)
async def fdh_cabinet_update(request: Request, cabinet_id: str, db: Session = Depends(get_db)):
    from app.models.network import FdhCabinet
    from app.services import catalog as catalog_service

    cabinet = db.query(FdhCabinet).filter(FdhCabinet.id == cabinet_id).first()
    if not cabinet:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "FDH Cabinet not found"},
            status_code=404,
        )

    before_snapshot = model_to_dict(cabinet)
    form = await request.form()
    name = form.get("name", "").strip()

    if not name:
        regions = catalog_service.region_zones.list(
            db=db, is_active=True, order_by="name", order_dir="asc", limit=500, offset=0
        )
        context = _base_context(request, db, active_page="fdh-cabinets", active_menu="fiber")
        context.update({
            "cabinet": cabinet,
            "regions": regions,
            "action_url": f"/admin/network/fdh-cabinets/{cabinet.id}",
            "error": "Cabinet name is required",
        })
        return templates.TemplateResponse("admin/network/fiber/fdh-cabinet-form.html", context)

    cabinet.name = name
    cabinet.code = form.get("code", "").strip() or None
    cabinet.region_id = form.get("region_id", "").strip() or None
    latitude = form.get("latitude", "").strip()
    longitude = form.get("longitude", "").strip()
    cabinet.latitude = float(latitude) if latitude else None
    cabinet.longitude = float(longitude) if longitude else None
    cabinet.notes = form.get("notes", "").strip() or None
    cabinet.is_active = form.get("is_active") == "true"

    db.commit()
    after_snapshot = model_to_dict(cabinet)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata = {"changes": changes} if changes else None
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="fdh_cabinet",
        entity_id=str(cabinet.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata,
    )

    return RedirectResponse(f"/admin/network/fdh-cabinets/{cabinet.id}", status_code=303)


@router.get("/fdh-cabinets/{cabinet_id}", response_class=HTMLResponse)
def fdh_cabinet_detail(request: Request, cabinet_id: str, db: Session = Depends(get_db)):
    from app.models.network import FdhCabinet, Splitter

    cabinet = db.query(FdhCabinet).filter(FdhCabinet.id == cabinet_id).first()
    if not cabinet:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "FDH Cabinet not found"},
            status_code=404,
        )

    splitters = db.query(Splitter).filter(Splitter.fdh_id == cabinet.id).order_by(Splitter.name).all()

    context = _base_context(request, db, active_page="fdh-cabinets", active_menu="fiber")
    context.update({
        "cabinet": cabinet,
        "splitters": splitters,
        "activities": _build_audit_activities(db, "fdh_cabinet", str(cabinet_id), limit=10),
    })
    return templates.TemplateResponse("admin/network/fiber/fdh-cabinet-detail.html", context)


@router.get("/splitters", response_class=HTMLResponse)
def splitters_list(request: Request, db: Session = Depends(get_db)):
    """List all splitters."""
    from app.models.network import Splitter

    splitters = db.query(Splitter).filter(Splitter.is_active == True).order_by(Splitter.name).limit(200).all()

    stats = {"total": len(splitters)}

    context = _base_context(request, db, active_page="splitters", active_menu="fiber")
    context.update({"splitters": splitters, "stats": stats})
    return templates.TemplateResponse("admin/network/fiber/splitters.html", context)


@router.get("/splitters/new", response_class=HTMLResponse)
def splitter_new(request: Request, fdh_id: str = None, db: Session = Depends(get_db)):
    from app.models.network import FdhCabinet

    cabinets = db.query(FdhCabinet).filter(FdhCabinet.is_active == True).order_by(FdhCabinet.name).all()

    context = _base_context(request, db, active_page="splitters", active_menu="fiber")
    context.update({
        "splitter": None,
        "cabinets": cabinets,
        "selected_fdh_id": fdh_id,
        "action_url": "/admin/network/splitters",
    })
    return templates.TemplateResponse("admin/network/fiber/splitter-form.html", context)


@router.post("/splitters", response_class=HTMLResponse)
async def splitter_create(request: Request, db: Session = Depends(get_db)):
    from app.models.network import FdhCabinet, Splitter

    form = await request.form()
    name = form.get("name", "").strip()
    fdh_id = form.get("fdh_id", "").strip() or None
    splitter_ratio = form.get("splitter_ratio", "").strip() or None
    notes = form.get("notes", "").strip() or None
    is_active = form.get("is_active") == "true"

    cabinets = db.query(FdhCabinet).filter(FdhCabinet.is_active == True).order_by(FdhCabinet.name).all()

    if not name:
        context = _base_context(request, db, active_page="splitters", active_menu="fiber")
        context.update({
            "splitter": None,
            "cabinets": cabinets,
            "selected_fdh_id": fdh_id,
            "action_url": "/admin/network/splitters",
            "error": "Splitter name is required",
        })
        return templates.TemplateResponse("admin/network/fiber/splitter-form.html", context)

    if fdh_id:
        cabinet = db.query(FdhCabinet).filter(FdhCabinet.id == fdh_id).first()
        if not cabinet:
            context = _base_context(request, db, active_page="splitters", active_menu="fiber")
            context.update({
                "splitter": None,
                "cabinets": cabinets,
                "selected_fdh_id": fdh_id,
                "action_url": "/admin/network/splitters",
                "error": "FDH cabinet not found",
            })
            return templates.TemplateResponse("admin/network/fiber/splitter-form.html", context)

    input_ports_raw = form.get("input_ports", "").strip()
    output_ports_raw = form.get("output_ports", "").strip()
    try:
        input_ports = int(input_ports_raw) if input_ports_raw else 1
    except ValueError:
        input_ports = 1
    try:
        output_ports = int(output_ports_raw) if output_ports_raw else 8
    except ValueError:
        output_ports = 8

    splitter = Splitter(
        name=name,
        fdh_id=fdh_id,
        splitter_ratio=splitter_ratio,
        input_ports=input_ports,
        output_ports=output_ports,
        notes=notes,
        is_active=is_active,
    )
    db.add(splitter)
    db.commit()
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="splitter",
        entity_id=str(splitter.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": splitter.name, "fdh_id": str(splitter.fdh_id) if splitter.fdh_id else None},
    )

    return RedirectResponse(f"/admin/network/splitters/{splitter.id}", status_code=303)


@router.get("/splitters/{splitter_id}/edit", response_class=HTMLResponse)
def splitter_edit(request: Request, splitter_id: str, db: Session = Depends(get_db)):
    from app.models.network import FdhCabinet, Splitter

    splitter = db.query(Splitter).filter(Splitter.id == splitter_id).first()
    if not splitter:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splitter not found"},
            status_code=404,
        )

    cabinets = db.query(FdhCabinet).filter(FdhCabinet.is_active == True).order_by(FdhCabinet.name).all()

    context = _base_context(request, db, active_page="splitters", active_menu="fiber")
    context.update({
        "splitter": splitter,
        "cabinets": cabinets,
        "selected_fdh_id": str(splitter.fdh_id) if splitter.fdh_id else None,
        "action_url": f"/admin/network/splitters/{splitter.id}",
    })
    return templates.TemplateResponse("admin/network/fiber/splitter-form.html", context)


@router.post("/splitters/{splitter_id}", response_class=HTMLResponse)
async def splitter_update(request: Request, splitter_id: str, db: Session = Depends(get_db)):
    from app.models.network import FdhCabinet, Splitter

    splitter = db.query(Splitter).filter(Splitter.id == splitter_id).first()
    if not splitter:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splitter not found"},
            status_code=404,
        )

    before_snapshot = model_to_dict(splitter)
    form = await request.form()
    name = form.get("name", "").strip()
    fdh_id = form.get("fdh_id", "").strip() or None
    splitter_ratio = form.get("splitter_ratio", "").strip() or None
    notes = form.get("notes", "").strip() or None
    is_active = form.get("is_active") == "true"

    cabinets = db.query(FdhCabinet).filter(FdhCabinet.is_active == True).order_by(FdhCabinet.name).all()

    if not name:
        context = _base_context(request, db, active_page="splitters", active_menu="fiber")
        context.update({
            "splitter": splitter,
            "cabinets": cabinets,
            "selected_fdh_id": fdh_id,
            "action_url": f"/admin/network/splitters/{splitter.id}",
            "error": "Splitter name is required",
        })
        return templates.TemplateResponse("admin/network/fiber/splitter-form.html", context)

    if fdh_id:
        cabinet = db.query(FdhCabinet).filter(FdhCabinet.id == fdh_id).first()
        if not cabinet:
            context = _base_context(request, db, active_page="splitters", active_menu="fiber")
            context.update({
                "splitter": splitter,
                "cabinets": cabinets,
                "selected_fdh_id": fdh_id,
                "action_url": f"/admin/network/splitters/{splitter.id}",
                "error": "FDH cabinet not found",
            })
            return templates.TemplateResponse("admin/network/fiber/splitter-form.html", context)

    input_ports_raw = form.get("input_ports", "").strip()
    output_ports_raw = form.get("output_ports", "").strip()
    try:
        input_ports = int(input_ports_raw) if input_ports_raw else splitter.input_ports
    except ValueError:
        input_ports = splitter.input_ports
    try:
        output_ports = int(output_ports_raw) if output_ports_raw else splitter.output_ports
    except ValueError:
        output_ports = splitter.output_ports

    splitter.name = name
    splitter.fdh_id = fdh_id
    splitter.splitter_ratio = splitter_ratio
    splitter.input_ports = input_ports
    splitter.output_ports = output_ports
    splitter.notes = notes
    splitter.is_active = is_active

    db.commit()
    after_snapshot = model_to_dict(splitter)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata = {"changes": changes} if changes else None
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="splitter",
        entity_id=str(splitter.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata,
    )

    return RedirectResponse(f"/admin/network/splitters/{splitter.id}", status_code=303)


@router.get("/splitters/{splitter_id}", response_class=HTMLResponse)
def splitter_detail(request: Request, splitter_id: str, db: Session = Depends(get_db)):
    from app.models.network import Splitter, SplitterPort

    splitter = db.query(Splitter).filter(Splitter.id == splitter_id).first()
    if not splitter:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splitter not found"},
            status_code=404,
        )

    ports = db.query(SplitterPort).filter(SplitterPort.splitter_id == splitter.id).order_by(SplitterPort.port_number).all()

    context = _base_context(request, db, active_page="splitters", active_menu="fiber")
    context.update({
        "splitter": splitter,
        "ports": ports,
        "activities": _build_audit_activities(db, "splitter", str(splitter_id), limit=10),
    })
    return templates.TemplateResponse("admin/network/fiber/splitter-detail.html", context)


@router.get("/fiber-strands", response_class=HTMLResponse)
def fiber_strands_list(request: Request, db: Session = Depends(get_db)):
    """List fiber strands."""
    from app.models.network import FiberStrand

    strands = db.query(FiberStrand).order_by(FiberStrand.cable_name, FiberStrand.strand_number).limit(200).all()

    stats = {
        "total": len(strands),
        "available": sum(1 for s in strands if s.status.value == "available"),
        "in_use": sum(1 for s in strands if s.status.value == "in_use"),
    }

    context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
    context.update({"strands": strands, "stats": stats})
    return templates.TemplateResponse("admin/network/fiber/strands.html", context)


@router.get("/fiber-strands/new", response_class=HTMLResponse)
def fiber_strand_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
    context.update({
        "strand": None,
        "action_url": "/admin/network/fiber-strands",
    })
    return templates.TemplateResponse("admin/network/fiber/strand-form.html", context)


@router.post("/fiber-strands", response_class=HTMLResponse)
async def fiber_strand_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    cable_name = form.get("cable_name", "").strip()
    strand_number_raw = form.get("strand_number", "").strip()
    label = form.get("label", "").strip()
    status = form.get("status", "").strip()
    upstream_type = form.get("upstream_type", "").strip()
    downstream_type = form.get("downstream_type", "").strip()
    notes = form.get("notes", "").strip()

    error = None
    if not cable_name:
        error = "Cable name is required."

    strand_number = None
    if not error:
        try:
            strand_number = int(strand_number_raw)
        except ValueError:
            error = "Strand number must be a valid integer."

    strand_data = {
        "cable_name": cable_name,
        "strand_number": strand_number_raw,
        "label": label or None,
        "status": {"value": status} if status else None,
        "upstream_type": {"value": upstream_type} if upstream_type else None,
        "downstream_type": {"value": downstream_type} if downstream_type else None,
        "notes": notes or None,
    }

    if error:
        context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
        context.update({
            "strand": strand_data,
            "action_url": "/admin/network/fiber-strands",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/fiber/strand-form.html", context)

    try:
        payload = FiberStrandCreate(
            cable_name=cable_name,
            strand_number=strand_number,
            label=label or None,
            status=status or None,
            upstream_type=upstream_type or None,
            downstream_type=downstream_type or None,
            notes=notes or None,
        )
        strand = network_service.fiber_strands.create(db=db, payload=payload)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="fiber_strand",
            entity_id=str(strand.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={
                "cable_name": strand.cable_name,
                "strand_number": strand.strand_number,
            },
        )
        return RedirectResponse("/admin/network/fiber-strands", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
    context.update({
        "strand": strand_data,
        "action_url": "/admin/network/fiber-strands",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/fiber/strand-form.html", context)


@router.get("/fiber-strands/{strand_id}/edit", response_class=HTMLResponse)
def fiber_strand_edit(request: Request, strand_id: str, db: Session = Depends(get_db)):
    strand = network_service.fiber_strands.get(db=db, strand_id=strand_id)
    context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
    context.update({
        "strand": strand,
        "action_url": f"/admin/network/fiber-strands/{strand_id}",
    })
    return templates.TemplateResponse("admin/network/fiber/strand-form.html", context)


@router.post("/fiber-strands/{strand_id}", response_class=HTMLResponse)
async def fiber_strand_update(request: Request, strand_id: str, db: Session = Depends(get_db)):
    strand = network_service.fiber_strands.get(db=db, strand_id=strand_id)

    form = await request.form()
    cable_name = form.get("cable_name", "").strip()
    strand_number_raw = form.get("strand_number", "").strip()
    label = form.get("label", "").strip()
    status = form.get("status", "").strip()
    upstream_type = form.get("upstream_type", "").strip()
    downstream_type = form.get("downstream_type", "").strip()
    notes = form.get("notes", "").strip()

    error = None
    if not cable_name:
        error = "Cable name is required."

    strand_number = None
    if not error:
        try:
            strand_number = int(strand_number_raw)
        except ValueError:
            error = "Strand number must be a valid integer."

    strand_data = {
        "id": strand.id,
        "cable_name": cable_name,
        "strand_number": strand_number_raw,
        "label": label or None,
        "status": {"value": status} if status else None,
        "upstream_type": {"value": upstream_type} if upstream_type else None,
        "downstream_type": {"value": downstream_type} if downstream_type else None,
        "notes": notes or None,
    }

    if error:
        context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
        context.update({
            "strand": strand_data,
            "action_url": f"/admin/network/fiber-strands/{strand_id}",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/fiber/strand-form.html", context)

    try:
        payload = FiberStrandUpdate(
            cable_name=cable_name,
            strand_number=strand_number,
            label=label or None,
            status=status or None,
            upstream_type=upstream_type or None,
            downstream_type=downstream_type or None,
            notes=notes or None,
        )
        before_snapshot = model_to_dict(strand)
        updated_strand = network_service.fiber_strands.update(
            db=db,
            strand_id=strand_id,
            payload=payload,
        )
        after_snapshot = model_to_dict(updated_strand)
        changes = diff_dicts(before_snapshot, after_snapshot)
        metadata = {"changes": changes} if changes else None
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="fiber_strand",
            entity_id=str(updated_strand.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata,
        )
        return RedirectResponse("/admin/network/fiber-strands", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="fiber-strands", active_menu="fiber")
    context.update({
        "strand": strand_data,
        "action_url": f"/admin/network/fiber-strands/{strand_id}",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/fiber/strand-form.html", context)


@router.get("/splice-closures", response_class=HTMLResponse)
def splice_closures_list(request: Request, db: Session = Depends(get_db)):
    """List splice closures."""
    from app.models.network import FiberSpliceClosure

    closures = db.query(FiberSpliceClosure).filter(FiberSpliceClosure.is_active == True).order_by(FiberSpliceClosure.name).limit(200).all()

    stats = {"total": len(closures)}

    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update({"closures": closures, "stats": stats})
    return templates.TemplateResponse("admin/network/fiber/splice-closures.html", context)


@router.get("/splice-closures/new", response_class=HTMLResponse)
def splice_closure_new(request: Request, db: Session = Depends(get_db)):
    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update({
        "closure": None,
        "action_url": "/admin/network/splice-closures",
    })
    return templates.TemplateResponse("admin/network/fiber/splice-closure-form.html", context)


@router.post("/splice-closures", response_class=HTMLResponse)
async def splice_closure_create(request: Request, db: Session = Depends(get_db)):
    from app.models.network import FiberSpliceClosure

    form = await request.form()
    name = form.get("name", "").strip()
    latitude = form.get("latitude", "").strip()
    longitude = form.get("longitude", "").strip()
    notes = form.get("notes", "").strip() or None
    is_active = form.get("is_active") == "true"

    if not name:
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update({
            "closure": None,
            "action_url": "/admin/network/splice-closures",
            "error": "Closure name is required",
        })
        return templates.TemplateResponse("admin/network/fiber/splice-closure-form.html", context)

    closure = FiberSpliceClosure(
        name=name,
        latitude=float(latitude) if latitude else None,
        longitude=float(longitude) if longitude else None,
        notes=notes,
        is_active=is_active,
    )
    db.add(closure)
    db.commit()
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="splice_closure",
        entity_id=str(closure.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": closure.name},
    )

    return RedirectResponse(f"/admin/network/splice-closures/{closure.id}", status_code=303)


@router.get("/splice-closures/{closure_id}/edit", response_class=HTMLResponse)
def splice_closure_edit(request: Request, closure_id: str, db: Session = Depends(get_db)):
    from app.models.network import FiberSpliceClosure

    closure = db.query(FiberSpliceClosure).filter(FiberSpliceClosure.id == closure_id).first()
    if not closure:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update({
        "closure": closure,
        "action_url": f"/admin/network/splice-closures/{closure.id}",
    })
    return templates.TemplateResponse("admin/network/fiber/splice-closure-form.html", context)


@router.post("/splice-closures/{closure_id}", response_class=HTMLResponse)
async def splice_closure_update(request: Request, closure_id: str, db: Session = Depends(get_db)):
    from app.models.network import FiberSpliceClosure

    closure = db.query(FiberSpliceClosure).filter(FiberSpliceClosure.id == closure_id).first()
    if not closure:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )

    before_snapshot = model_to_dict(closure)
    form = await request.form()
    name = form.get("name", "").strip()

    if not name:
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update({
            "closure": closure,
            "action_url": f"/admin/network/splice-closures/{closure.id}",
            "error": "Closure name is required",
        })
        return templates.TemplateResponse("admin/network/fiber/splice-closure-form.html", context)

    closure.name = name
    latitude = form.get("latitude", "").strip()
    longitude = form.get("longitude", "").strip()
    closure.latitude = float(latitude) if latitude else None
    closure.longitude = float(longitude) if longitude else None
    closure.notes = form.get("notes", "").strip() or None
    closure.is_active = form.get("is_active") == "true"

    db.commit()
    after_snapshot = model_to_dict(closure)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata = {"changes": changes} if changes else None
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="splice_closure",
        entity_id=str(closure.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata,
    )

    return RedirectResponse(f"/admin/network/splice-closures/{closure.id}", status_code=303)


@router.get("/splice-closures/{closure_id}", response_class=HTMLResponse)
def splice_closure_detail(request: Request, closure_id: str, db: Session = Depends(get_db)):
    from app.models.network import FiberSpliceClosure, FiberSpliceTray, FiberSplice

    closure = db.query(FiberSpliceClosure).filter(FiberSpliceClosure.id == closure_id).first()
    if not closure:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )

    trays = db.query(FiberSpliceTray).filter(FiberSpliceTray.closure_id == closure.id).order_by(FiberSpliceTray.tray_number).all()
    splices = db.query(FiberSplice).filter(FiberSplice.closure_id == closure.id).all()

    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update({
        "closure": closure,
        "trays": trays,
        "splices": splices,
        "activities": _build_audit_activities(db, "splice_closure", str(closure_id), limit=10),
    })
    return templates.TemplateResponse("admin/network/fiber/splice-closure-detail.html", context)


@router.get("/splice-closures/{closure_id}/trays/new", response_class=HTMLResponse)
def splice_tray_new(request: Request, closure_id: str, db: Session = Depends(get_db)):
    from app.models.network import FiberSpliceClosure

    closure = db.query(FiberSpliceClosure).filter(FiberSpliceClosure.id == closure_id).first()
    if not closure:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update({
        "closure": closure,
        "tray": None,
        "action_url": f"/admin/network/splice-closures/{closure_id}/trays",
    })
    return templates.TemplateResponse("admin/network/fiber/splice-tray-form.html", context)


@router.get("/splice-closures/{closure_id}/trays", response_class=HTMLResponse)
def splice_tray_redirect(closure_id: str):
    return RedirectResponse(f"/admin/network/splice-closures/{closure_id}", status_code=303)


@router.get("/splice-closures/{closure_id}/trays/{tray_id}/edit", response_class=HTMLResponse)
def splice_tray_edit(
    request: Request,
    closure_id: str,
    tray_id: str,
    db: Session = Depends(get_db),
):
    from app.models.network import FiberSpliceClosure, FiberSpliceTray

    closure = db.query(FiberSpliceClosure).filter(FiberSpliceClosure.id == closure_id).first()
    tray = db.query(FiberSpliceTray).filter(
        FiberSpliceTray.id == tray_id,
        FiberSpliceTray.closure_id == closure_id,
    ).first()
    if not closure or not tray:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Tray not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update({
        "closure": closure,
        "tray": tray,
        "action_url": f"/admin/network/splice-closures/{closure_id}/trays/{tray_id}",
    })
    return templates.TemplateResponse("admin/network/fiber/splice-tray-form.html", context)


@router.post("/splice-closures/{closure_id}/trays", response_class=HTMLResponse)
async def splice_tray_create(request: Request, closure_id: str, db: Session = Depends(get_db)):
    from app.models.network import FiberSpliceClosure, FiberSpliceTray

    closure = db.query(FiberSpliceClosure).filter(FiberSpliceClosure.id == closure_id).first()
    if not closure:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )

    form = await request.form()
    tray_number_raw = form.get("tray_number", "").strip()
    name = form.get("name", "").strip()
    notes = form.get("notes", "").strip()

    error = None
    try:
        tray_number = int(tray_number_raw)
    except ValueError:
        tray_number = 0
        error = "Tray number must be a valid integer."

    tray_data = {
        "tray_number": tray_number_raw,
        "name": name,
        "notes": notes or None,
    }

    if error or tray_number <= 0:
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update({
            "closure": closure,
            "tray": tray_data,
            "action_url": f"/admin/network/splice-closures/{closure_id}/trays",
            "error": error or "Tray number must be greater than 0.",
        })
        return templates.TemplateResponse("admin/network/fiber/splice-tray-form.html", context)

    try:
        tray = FiberSpliceTray(
            closure_id=closure.id,
            tray_number=tray_number,
            name=name or None,
            notes=notes or None,
        )
        db.add(tray)
        db.commit()
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="tray_created",
            entity_type="splice_closure",
            entity_id=str(closure.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"tray_number": tray.tray_number, "tray_name": tray.name},
        )
        return RedirectResponse(f"/admin/network/splice-closures/{closure.id}", status_code=303)
    except Exception as exc:
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update({
            "closure": closure,
            "tray": tray_data,
            "action_url": f"/admin/network/splice-closures/{closure_id}/trays",
            "error": str(exc),
        })
        return templates.TemplateResponse("admin/network/fiber/splice-tray-form.html", context)


@router.post("/splice-closures/{closure_id}/trays/{tray_id}", response_class=HTMLResponse)
async def splice_tray_update(
    request: Request,
    closure_id: str,
    tray_id: str,
    db: Session = Depends(get_db),
):
    from app.models.network import FiberSpliceClosure, FiberSpliceTray

    closure = db.query(FiberSpliceClosure).filter(FiberSpliceClosure.id == closure_id).first()
    tray = db.query(FiberSpliceTray).filter(
        FiberSpliceTray.id == tray_id,
        FiberSpliceTray.closure_id == closure_id,
    ).first()
    if not closure or not tray:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Tray not found"},
            status_code=404,
        )

    form = await request.form()
    tray_number_raw = form.get("tray_number", "").strip()
    name = form.get("name", "").strip()
    notes = form.get("notes", "").strip()

    error = None
    try:
        tray_number = int(tray_number_raw)
    except ValueError:
        tray_number = 0
        error = "Tray number must be a valid integer."

    tray_data = {
        "id": tray.id,
        "tray_number": tray_number_raw,
        "name": name,
        "notes": notes or None,
    }

    if error or tray_number <= 0:
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update({
            "closure": closure,
            "tray": tray_data,
            "action_url": f"/admin/network/splice-closures/{closure_id}/trays/{tray_id}",
            "error": error or "Tray number must be greater than 0.",
        })
        return templates.TemplateResponse("admin/network/fiber/splice-tray-form.html", context)

    try:
        tray.tray_number = tray_number
        tray.name = name or None
        tray.notes = notes or None
        db.commit()
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="tray_updated",
            entity_type="splice_closure",
            entity_id=str(closure.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={"tray_number": tray.tray_number, "tray_name": tray.name},
        )
        return RedirectResponse(f"/admin/network/splice-closures/{closure.id}", status_code=303)
    except Exception as exc:
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update({
            "closure": closure,
            "tray": tray_data,
            "action_url": f"/admin/network/splice-closures/{closure_id}/trays/{tray_id}",
            "error": str(exc),
        })
        return templates.TemplateResponse("admin/network/fiber/splice-tray-form.html", context)


@router.get("/splice-closures/{closure_id}/splices/new", response_class=HTMLResponse)
def splice_new(request: Request, closure_id: str, db: Session = Depends(get_db)):
    from app.models.network import FiberSpliceClosure, FiberSpliceTray, FiberStrand

    closure = db.query(FiberSpliceClosure).filter(FiberSpliceClosure.id == closure_id).first()
    if not closure:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )

    trays = db.query(FiberSpliceTray).filter(FiberSpliceTray.closure_id == closure.id).order_by(
        FiberSpliceTray.tray_number
    ).all()
    strands = db.query(FiberStrand).order_by(FiberStrand.cable_name, FiberStrand.strand_number).limit(500).all()

    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update({
        "closure": closure,
        "trays": trays,
        "strands": strands,
        "splice": None,
        "action_url": f"/admin/network/splice-closures/{closure_id}/splices",
    })
    return templates.TemplateResponse("admin/network/fiber/splice-form.html", context)


@router.post("/splice-closures/{closure_id}/splices", response_class=HTMLResponse)
async def splice_create(request: Request, closure_id: str, db: Session = Depends(get_db)):
    from app.models.network import FiberSpliceClosure, FiberSpliceTray, FiberStrand

    closure = db.query(FiberSpliceClosure).filter(FiberSpliceClosure.id == closure_id).first()
    if not closure:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splice Closure not found"},
            status_code=404,
        )

    trays = db.query(FiberSpliceTray).filter(FiberSpliceTray.closure_id == closure.id).order_by(
        FiberSpliceTray.tray_number
    ).all()
    strands = db.query(FiberStrand).order_by(FiberStrand.cable_name, FiberStrand.strand_number).limit(500).all()

    form = await request.form()
    from_strand_id = form.get("from_strand_id", "").strip()
    to_strand_id = form.get("to_strand_id", "").strip()
    tray_id = form.get("tray_id", "").strip()
    splice_type = form.get("splice_type", "").strip()
    loss_db_raw = form.get("loss_db", "").strip()
    notes = form.get("notes", "").strip()

    error = None
    if not from_strand_id or not to_strand_id:
        error = "Both from and to strands are required."
    elif from_strand_id == to_strand_id:
        error = "From and to strands must be different."

    loss_db = None
    if not error and loss_db_raw:
        try:
            loss_db = float(loss_db_raw)
        except ValueError:
            error = "Loss must be a valid number."

    splice_data = {
        "from_strand_id": from_strand_id,
        "to_strand_id": to_strand_id,
        "tray_id": tray_id or None,
        "splice_type": splice_type or None,
        "loss_db": loss_db_raw,
        "notes": notes or None,
    }

    if error:
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update({
            "closure": closure,
            "trays": trays,
            "strands": strands,
            "splice": splice_data,
            "action_url": f"/admin/network/splice-closures/{closure_id}/splices",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/fiber/splice-form.html", context)

    try:
        payload = FiberSpliceCreate(
            closure_id=closure.id,
            from_strand_id=from_strand_id,
            to_strand_id=to_strand_id,
            tray_id=tray_id or None,
            splice_type=splice_type or None,
            loss_db=loss_db,
            notes=notes or None,
        )
        splice = network_service.fiber_splices.create(db=db, payload=payload)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="splice_created",
            entity_type="splice_closure",
            entity_id=str(closure.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={
                "from_strand_id": str(splice.from_strand_id) if splice.from_strand_id else None,
                "to_strand_id": str(splice.to_strand_id) if splice.to_strand_id else None,
            },
        )
        return RedirectResponse(f"/admin/network/splice-closures/{closure.id}", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update({
        "closure": closure,
        "trays": trays,
        "strands": strands,
        "splice": splice_data,
        "action_url": f"/admin/network/splice-closures/{closure_id}/splices",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/fiber/splice-form.html", context)


@router.get("/splice-closures/{closure_id}/splices/{splice_id}/edit", response_class=HTMLResponse)
def splice_edit(
    request: Request,
    closure_id: str,
    splice_id: str,
    db: Session = Depends(get_db),
):
    from app.models.network import FiberSplice, FiberSpliceClosure, FiberSpliceTray, FiberStrand

    closure = db.query(FiberSpliceClosure).filter(FiberSpliceClosure.id == closure_id).first()
    splice = db.query(FiberSplice).filter(
        FiberSplice.id == splice_id,
        FiberSplice.closure_id == closure_id,
    ).first()
    if not closure or not splice:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Fiber splice not found"},
            status_code=404,
        )

    trays = db.query(FiberSpliceTray).filter(FiberSpliceTray.closure_id == closure.id).order_by(
        FiberSpliceTray.tray_number
    ).all()
    strands = db.query(FiberStrand).order_by(FiberStrand.cable_name, FiberStrand.strand_number).limit(500).all()

    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update({
        "closure": closure,
        "trays": trays,
        "strands": strands,
        "splice": splice,
        "action_url": f"/admin/network/splice-closures/{closure_id}/splices/{splice_id}",
    })
    return templates.TemplateResponse("admin/network/fiber/splice-form.html", context)


@router.post("/splice-closures/{closure_id}/splices/{splice_id}", response_class=HTMLResponse)
async def splice_update(
    request: Request,
    closure_id: str,
    splice_id: str,
    db: Session = Depends(get_db),
):
    from app.models.network import FiberSplice, FiberSpliceClosure, FiberSpliceTray, FiberStrand

    closure = db.query(FiberSpliceClosure).filter(FiberSpliceClosure.id == closure_id).first()
    splice = db.query(FiberSplice).filter(
        FiberSplice.id == splice_id,
        FiberSplice.closure_id == closure_id,
    ).first()
    if not closure or not splice:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Fiber splice not found"},
            status_code=404,
        )

    trays = db.query(FiberSpliceTray).filter(FiberSpliceTray.closure_id == closure.id).order_by(
        FiberSpliceTray.tray_number
    ).all()
    strands = db.query(FiberStrand).order_by(FiberStrand.cable_name, FiberStrand.strand_number).limit(500).all()

    form = await request.form()
    from_strand_id = form.get("from_strand_id", "").strip()
    to_strand_id = form.get("to_strand_id", "").strip()
    tray_id = form.get("tray_id", "").strip()
    splice_type = form.get("splice_type", "").strip()
    loss_db_raw = form.get("loss_db", "").strip()
    notes = form.get("notes", "").strip()

    error = None
    if not from_strand_id or not to_strand_id:
        error = "Both from and to strands are required."
    elif from_strand_id == to_strand_id:
        error = "From and to strands must be different."

    loss_db = None
    if not error and loss_db_raw:
        try:
            loss_db = float(loss_db_raw)
        except ValueError:
            error = "Loss must be a valid number."

    splice_data = {
        "id": splice.id,
        "from_strand_id": from_strand_id,
        "to_strand_id": to_strand_id,
        "tray_id": tray_id or None,
        "splice_type": splice_type or None,
        "loss_db": loss_db_raw,
        "notes": notes or None,
    }

    if error:
        context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
        context.update({
            "closure": closure,
            "trays": trays,
            "strands": strands,
            "splice": splice_data,
            "action_url": f"/admin/network/splice-closures/{closure_id}/splices/{splice_id}",
            "error": error,
        })
        return templates.TemplateResponse("admin/network/fiber/splice-form.html", context)

    try:
        payload = FiberSpliceUpdate(
            from_strand_id=from_strand_id,
            to_strand_id=to_strand_id,
            tray_id=tray_id or None,
            splice_type=splice_type or None,
            loss_db=loss_db,
            notes=notes or None,
        )
        updated_splice = network_service.fiber_splices.update(
            db=db,
            splice_id=splice_id,
            payload=payload,
        )
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="splice_updated",
            entity_type="splice_closure",
            entity_id=str(closure.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={
                "from_strand_id": str(updated_splice.from_strand_id) if updated_splice.from_strand_id else None,
                "to_strand_id": str(updated_splice.to_strand_id) if updated_splice.to_strand_id else None,
            },
        )
        return RedirectResponse(f"/admin/network/splice-closures/{closure.id}", status_code=303)
    except ValidationError as exc:
        error = exc.errors()[0]["msg"]
    except Exception as exc:
        error = str(exc)

    context = _base_context(request, db, active_page="splice-closures", active_menu="fiber")
    context.update({
        "closure": closure,
        "trays": trays,
        "strands": strands,
        "splice": splice_data,
        "action_url": f"/admin/network/splice-closures/{closure_id}/splices/{splice_id}",
        "error": error or "Please correct the highlighted fields.",
    })
    return templates.TemplateResponse("admin/network/fiber/splice-form.html", context)

# ==================== Comprehensive Network Map ====================

@router.get("/map", response_class=HTMLResponse)
def comprehensive_network_map(request: Request, db: Session = Depends(get_db)):
    """Comprehensive network map showing all infrastructure and customers."""
    from app.services import network_map as network_map_service

    context = _base_context(request, db, active_page="network-map")
    context.update(network_map_service.build_network_map_context(db))
    return templates.TemplateResponse("admin/network/map.html", context)


# ---- Wireless Site Survey Routes ----

@router.get("/site-survey", response_class=HTMLResponse)
def site_survey_list(request: Request, db: Session = Depends(get_db)):
    """List wireless site surveys."""
    from app.services import wireless_survey as ws_service

    surveys = ws_service.wireless_surveys.list(db, limit=100)
    context = _base_context(request, db, active_page="site-survey")
    context.update({
        "surveys": surveys,
    })
    return templates.TemplateResponse("admin/network/site-survey/index.html", context)


@router.get("/site-survey/new", response_class=HTMLResponse)
def site_survey_new(
    request: Request,
    lat: float | None = None,
    lon: float | None = None,
    subscriber_id: str | None = None,
    db: Session = Depends(get_db),
):
    """Create new wireless site survey page."""
    from app.services import wireless_survey as ws_service

    context = _base_context(request, db, active_page="site-survey")
    context.update(
        ws_service.wireless_surveys.build_form_context(
            db, None, lat, lon, subscriber_id
        )
    )
    return templates.TemplateResponse("admin/network/site-survey/form.html", context)


@router.post("/site-survey/new", response_class=HTMLResponse)
def site_survey_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(None),
    frequency_mhz: float = Form(None),
    default_antenna_height_m: float = Form(10.0),
    default_tx_power_dbm: float = Form(20.0),
    project_id: str = Form(None),
    initial_lat: float | None = Form(None),
    initial_lon: float | None = Form(None),
    subscriber_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Create new wireless site survey."""
    from app.services import wireless_survey as ws_service
    actor_id = getattr(request.state, "actor_id", None)
    survey = ws_service.wireless_surveys.create_from_form(
        db,
        name,
        description,
        frequency_mhz,
        default_antenna_height_m,
        default_tx_power_dbm,
        project_id,
        subscriber_id,
        actor_id,
    )
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="site_survey",
        entity_id=str(survey.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": survey.name},
    )
    redirect_url = ws_service.wireless_surveys.build_post_create_redirect(
        survey.id, initial_lat, initial_lon
    )
    return RedirectResponse(redirect_url, status_code=303)


@router.get("/site-survey/{survey_id}", response_class=HTMLResponse)
def site_survey_detail(request: Request, survey_id: str, db: Session = Depends(get_db)):
    """Wireless site survey detail with interactive map."""
    from app.services import wireless_survey as ws_service

    context = _base_context(request, db, active_page="site-survey")
    context.update(ws_service.wireless_surveys.build_detail_context(db, survey_id))
    context["activities"] = _build_audit_activities(db, "site_survey", str(survey_id), limit=10)
    return templates.TemplateResponse("admin/network/site-survey/detail.html", context)


@router.get("/site-survey/{survey_id}/edit", response_class=HTMLResponse)
def site_survey_edit(request: Request, survey_id: str, db: Session = Depends(get_db)):
    """Edit wireless site survey."""
    from app.services import wireless_survey as ws_service

    survey = ws_service.wireless_surveys.get(db, survey_id)
    context = _base_context(request, db, active_page="site-survey")
    context.update(
        ws_service.wireless_surveys.build_form_context(db, survey, None, None, None)
    )
    return templates.TemplateResponse("admin/network/site-survey/form.html", context)


@router.post("/site-survey/{survey_id}/edit", response_class=HTMLResponse)
def site_survey_update(
    request: Request,
    survey_id: str,
    name: str = Form(...),
    description: str = Form(None),
    frequency_mhz: float = Form(None),
    default_antenna_height_m: float = Form(10.0),
    default_tx_power_dbm: float = Form(20.0),
    project_id: str = Form(None),
    status: str = Form("draft"),
    db: Session = Depends(get_db),
):
    """Update wireless site survey."""
    from app.services import wireless_survey as ws_service
    from app.schemas.wireless_survey import WirelessSiteSurveyUpdate
    from app.models.wireless_survey import SurveyStatus
    from uuid import UUID

    existing_survey = ws_service.wireless_surveys.get(db, survey_id)
    before_snapshot = model_to_dict(existing_survey)
    payload = WirelessSiteSurveyUpdate(
        name=name,
        description=description,
        frequency_mhz=frequency_mhz,
        default_antenna_height_m=default_antenna_height_m,
        default_tx_power_dbm=default_tx_power_dbm,
        project_id=UUID(project_id) if project_id else None,
        status=SurveyStatus(status),
    )
    updated_survey = ws_service.wireless_surveys.update(db, survey_id, payload)
    after_snapshot = model_to_dict(updated_survey)
    changes = diff_dicts(before_snapshot, after_snapshot)
    metadata = {"changes": changes} if changes else None
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="site_survey",
        entity_id=str(updated_survey.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata=metadata,
    )
    return RedirectResponse(f"/admin/network/site-survey/{survey_id}", status_code=303)


@router.post("/site-survey/{survey_id}/delete")
def site_survey_delete(request: Request, survey_id: str, db: Session = Depends(get_db)):
    """Delete wireless site survey."""
    from app.services import wireless_survey as ws_service

    survey = ws_service.wireless_surveys.get(db, survey_id)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="delete",
        entity_type="site_survey",
        entity_id=str(survey.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"name": survey.name},
    )
    ws_service.wireless_surveys.delete(db, survey_id)
    return RedirectResponse("/admin/network/site-survey", status_code=303)


@router.get("/site-survey/{survey_id}/elevation", response_class=HTMLResponse)
def site_survey_elevation_lookup(
    request: Request,
    survey_id: str,
    lat: float,
    lon: float,
    db: Session = Depends(get_db),
):
    """Get elevation for a point (HTMX endpoint)."""
    from fastapi.responses import JSONResponse
    from app.services import dem as dem_service

    result = dem_service.get_elevation(lat, lon)
    return JSONResponse(result)


@router.post("/site-survey/{survey_id}/points")
def site_survey_add_point(
    request: Request,
    survey_id: str,
    name: str = Form(...),
    latitude: float = Form(...),
    longitude: float = Form(...),
    point_type: str = Form("custom"),
    antenna_height_m: float = Form(10.0),
    db: Session = Depends(get_db),
):
    """Add a point to a survey."""
    from app.services import wireless_survey as ws_service
    from app.schemas.wireless_survey import SurveyPointCreate
    from app.models.wireless_survey import SurveyPointType

    payload = SurveyPointCreate(
        name=name,
        latitude=latitude,
        longitude=longitude,
        point_type=SurveyPointType(point_type),
        antenna_height_m=antenna_height_m,
    )
    point = ws_service.survey_points.create(db, survey_id, payload)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="point_added",
        entity_type="site_survey",
        entity_id=str(survey_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={
            "point": point.name,
            "point_type": point.point_type.value if point.point_type else None,
        },
    )
    return RedirectResponse(f"/admin/network/site-survey/{survey_id}", status_code=303)


@router.post("/site-survey/points/{point_id}/delete")
def site_survey_delete_point(request: Request, point_id: str, db: Session = Depends(get_db)):
    """Delete a survey point."""
    from app.services import wireless_survey as ws_service

    point = ws_service.survey_points.get(db, point_id)
    survey_id = point.survey_id
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="point_deleted",
        entity_type="site_survey",
        entity_id=str(survey_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={
            "point": point.name,
            "point_type": point.point_type.value if point.point_type else None,
        },
    )
    ws_service.survey_points.delete(db, point_id)
    return RedirectResponse(f"/admin/network/site-survey/{survey_id}", status_code=303)


@router.post("/site-survey/{survey_id}/analyze-los")
def site_survey_analyze_los(
    request: Request,
    survey_id: str,
    from_point_id: str = Form(...),
    to_point_id: str = Form(...),
    db: Session = Depends(get_db),
):
    """Analyze LOS between two points."""
    from app.services import wireless_survey as ws_service

    los_path = ws_service.survey_los.analyze_path(db, survey_id, from_point_id, to_point_id)
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="los_analyzed",
        entity_type="site_survey",
        entity_id=str(survey_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={
            "from_point_id": str(los_path.from_point_id),
            "to_point_id": str(los_path.to_point_id),
            "has_clear_los": los_path.has_clear_los,
        },
    )
    return RedirectResponse(f"/admin/network/site-survey/{survey_id}", status_code=303)


@router.get("/site-survey/{survey_id}/los/{path_id}")
def site_survey_los_detail(request: Request, survey_id: str, path_id: str, db: Session = Depends(get_db)):
    """Get LOS path detail with elevation profile (JSON)."""
    from fastapi.responses import JSONResponse
    from app.services import wireless_survey as ws_service

    los_path = ws_service.survey_los.get(db, path_id)
    return JSONResponse({
        "id": str(los_path.id),
        "from_point_id": str(los_path.from_point_id),
        "to_point_id": str(los_path.to_point_id),
        "distance_m": los_path.distance_m,
        "bearing_deg": los_path.bearing_deg,
        "has_clear_los": los_path.has_clear_los,
        "fresnel_clearance_pct": los_path.fresnel_clearance_pct,
        "max_obstruction_m": los_path.max_obstruction_m,
        "obstruction_distance_m": los_path.obstruction_distance_m,
        "free_space_loss_db": los_path.free_space_loss_db,
        "estimated_rssi_dbm": los_path.estimated_rssi_dbm,
        "elevation_profile": los_path.elevation_profile,
        "sample_count": los_path.sample_count,
    })


@router.post("/site-survey/los/{path_id}/delete")
def site_survey_delete_los(request: Request, path_id: str, db: Session = Depends(get_db)):
    """Delete a LOS path."""
    from app.services import wireless_survey as ws_service

    los_path = ws_service.survey_los.get(db, path_id)
    survey_id = los_path.survey_id
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="los_deleted",
        entity_type="site_survey",
        entity_id=str(survey_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={
            "from_point_id": str(los_path.from_point_id),
            "to_point_id": str(los_path.to_point_id),
        },
    )
    ws_service.survey_los.delete(db, path_id)
    return RedirectResponse(f"/admin/network/site-survey/{survey_id}", status_code=303)
