import argparse
import json
import math
import zipfile
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Iterable
from xml.etree import ElementTree as ET

try:
    from dotenv import load_dotenv as _load_dotenv
except ImportError:  # pragma: no cover - optional dependency for local env files
    load_dotenv: Callable[..., bool] | None = None
else:
    load_dotenv = _load_dotenv
from sqlalchemy import func

from app.db import SessionLocal
from app.models.gis import ServiceBuilding
from app.models.network import (
    FiberAccessPoint,
    FiberCableType,
    FiberSegment,
    FiberSegmentType,
    FiberSplice,
    FiberSpliceClosure,
    FiberSpliceTray,
    FdhCabinet,
    PonPortSplitterLink,
    Splitter,
    SplitterPort,
    SplitterPortAssignment,
)
from app.models.vendor import AsBuiltRoute
from app.models.wireless_mast import WirelessMast


KML_NS = {"kml": "http://www.opengis.net/kml/2.2"}


@dataclass
class PlacemarkData:
    name: str
    properties: dict[str, str | None]
    geometry_type: str
    coordinates: list[tuple[float, float]]


def parse_args():
    parser = argparse.ArgumentParser(description="Import KMZ/KML data into fiber plant tables.")
    parser.add_argument("--paths-kmz", action="append", default=[], help="KMZ with fiber paths (LineString).")
    parser.add_argument("--cabinet-kmz", action="append", default=[], help="KMZ with cabinets (Polygon/Point).")
    parser.add_argument("--splice-kmz", action="append", default=[], help="KMZ with splice closures (Polygon/Point).")
    parser.add_argument("--access-point-kmz", action="append", default=[], help="KMZ with fiber access points (Polygon/Point).")
    parser.add_argument("--mast-kmz", action="append", default=[], help="KMZ with wireless masts/poles (Point).")
    parser.add_argument("--building-kmz", action="append", default=[], help="KMZ with service buildings (Polygon/Point).")
    parser.add_argument("--segment-type", default="distribution", choices=[t.value for t in FiberSegmentType])
    parser.add_argument("--cable-type", default=None, choices=[t.value for t in FiberCableType])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--upsert", action="store_true", help="Update existing rows instead of skipping.")
    parser.add_argument(
        "--purge",
        action="store_true",
        help="Delete existing FiberSegment/FdhCabinet/FiberSpliceClosure rows before import.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Limit placemarks per file.")
    return parser.parse_args()


def _read_kmz_kml(path: Path) -> ET.Element:
    with zipfile.ZipFile(path) as kmz:
        kml_name = next((n for n in kmz.namelist() if n.lower().endswith(".kml")), None)
        if not kml_name:
            raise ValueError(f"No KML found inside {path}")
        return ET.fromstring(kmz.read(kml_name))


def _collect_properties(placemark: ET.Element) -> dict[str, str | None]:
    props: dict[str, str | None] = {}
    for simple in placemark.findall(".//kml:SimpleData", KML_NS):
        key = simple.attrib.get("name")
        if not key:
            continue
        value = (simple.text or "").strip()
        props[key] = value or None
    for data_el in placemark.findall(".//kml:Data", KML_NS):
        key = data_el.attrib.get("name")
        if not key:
            continue
        value = data_el.findtext("kml:value", default="", namespaces=KML_NS).strip()
        props[key] = value or None
    return props


def _parse_coord_text(text: str) -> list[tuple[float, float]]:
    coords: list[tuple[float, float]] = []
    for token in text.strip().split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            lon = float(parts[0])
            lat = float(parts[1])
        except ValueError:
            continue
        coords.append((lon, lat))
    return coords


def _extract_geometry(placemark: ET.Element) -> tuple[str, list[tuple[float, float]]] | None:
    point = placemark.find(".//kml:Point", KML_NS)
    if point is not None:
        text = point.findtext("kml:coordinates", default="", namespaces=KML_NS)
        coords = _parse_coord_text(text)
        return ("Point", coords)
    line = placemark.find(".//kml:LineString", KML_NS)
    if line is not None:
        text = line.findtext("kml:coordinates", default="", namespaces=KML_NS)
        coords = _parse_coord_text(text)
        return ("LineString", coords)
    polygon = placemark.find(".//kml:Polygon", KML_NS)
    if polygon is not None:
        text = polygon.findtext(".//kml:coordinates", default="", namespaces=KML_NS)
        coords = _parse_coord_text(text)
        return ("Polygon", coords)
    return None


def _iter_placemarks(root: ET.Element, limit: int | None = None) -> Iterable[PlacemarkData]:
    count = 0
    for placemark in root.findall(".//kml:Placemark", KML_NS):
        name = placemark.findtext("kml:name", default="", namespaces=KML_NS).strip()
        geom = _extract_geometry(placemark)
        if geom is None:
            continue
        geometry_type, coords = geom
        if not coords:
            continue
        properties = _collect_properties(placemark)
        yield PlacemarkData(name=name, properties=properties, geometry_type=geometry_type, coordinates=coords)
        count += 1
        if limit is not None and count >= limit:
            break


def _polygon_centroid(coords: list[tuple[float, float]]) -> tuple[float, float]:
    if coords[0] != coords[-1]:
        coords = coords + [coords[0]]
    area = 0.0
    cx = 0.0
    cy = 0.0
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(area) < 1e-12:
        avg_lon = sum(x for x, _ in coords) / len(coords)
        avg_lat = sum(y for _, y in coords) / len(coords)
        return avg_lon, avg_lat
    area *= 0.5
    cx /= (6.0 * area)
    cy /= (6.0 * area)
    return cx, cy


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    rad = math.radians
    dlat = rad(lat2 - lat1)
    dlon = rad(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rad(lat1)) * math.cos(rad(lat2)) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(a))


def _line_length_m(coords: list[tuple[float, float]]) -> float:
    total = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        total += _haversine_m(lon1, lat1, lon2, lat2)
    return total


def _geojson_to_geom(geojson: dict) -> object:
    geojson_str = json.dumps(geojson)
    return func.ST_SetSRID(func.ST_GeomFromGeoJSON(geojson_str), 4326)


def _point_geom(lon: float, lat: float) -> object:
    return func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326)


def _make_notes(properties: dict[str, str | None]) -> str | None:
    if not properties:
        return None
    return json.dumps(properties, ensure_ascii=True, sort_keys=True)


def import_segments(db, paths: list[Path], segment_type: str, cable_type: str | None, upsert: bool, limit: int | None):
    created = 0
    updated = 0
    skipped = 0
    for path in paths:
        root = _read_kmz_kml(path)
        for placemark in _iter_placemarks(root, limit=limit):
            if placemark.geometry_type != "LineString":
                continue
            name = placemark.name or placemark.properties.get("spanid") or "unnamed-segment"
            existing = db.query(FiberSegment).filter(FiberSegment.name == name).first()
            coords = placemark.coordinates
            geojson = {"type": "LineString", "coordinates": coords}
            length_m = _line_length_m(coords) if len(coords) > 1 else None
            notes = _make_notes(placemark.properties)
            if existing:
                if not upsert:
                    skipped += 1
                    continue
                existing.segment_type = FiberSegmentType(segment_type)
                existing.cable_type = FiberCableType(cable_type) if cable_type else None
                existing.route_geom = _geojson_to_geom(geojson)
                existing.length_m = length_m
                existing.notes = notes
                updated += 1
            else:
                segment = FiberSegment(
                    name=name,
                    segment_type=FiberSegmentType(segment_type),
                    cable_type=FiberCableType(cable_type) if cable_type else None,
                    route_geom=_geojson_to_geom(geojson),
                    length_m=length_m,
                    notes=notes,
                )
                db.add(segment)
                created += 1
    return created, updated, skipped


def _extract_point(placemark: PlacemarkData) -> tuple[float, float] | None:
    if placemark.geometry_type == "Point":
        return placemark.coordinates[0]
    if placemark.geometry_type == "Polygon":
        return _polygon_centroid(placemark.coordinates)
    return None


def import_cabinets(db, paths: list[Path], upsert: bool, limit: int | None):
    created = 0
    updated = 0
    skipped = 0
    for path in paths:
        root = _read_kmz_kml(path)
        for placemark in _iter_placemarks(root, limit=limit):
            point = _extract_point(placemark)
            if not point:
                continue
            lon, lat = point
            name = placemark.properties.get("name") or placemark.name or "unnamed-cabinet"
            code = placemark.properties.get("fibermngrid")
            existing = None
            if code:
                existing = db.query(FdhCabinet).filter(FdhCabinet.code == code).first()
            if not existing:
                existing = db.query(FdhCabinet).filter(FdhCabinet.name == name).first()
            notes = _make_notes(placemark.properties)
            if existing:
                if not upsert:
                    skipped += 1
                    continue
                existing.name = name
                existing.code = code
                existing.latitude = lat
                existing.longitude = lon
                existing.geom = _point_geom(lon, lat)
                existing.notes = notes
                updated += 1
            else:
                cabinet = FdhCabinet(
                    name=name,
                    code=code,
                    latitude=lat,
                    longitude=lon,
                    geom=_point_geom(lon, lat),
                    notes=notes,
                )
                db.add(cabinet)
                created += 1
    return created, updated, skipped


def import_splice_closures(db, paths: list[Path], upsert: bool, limit: int | None):
    created = 0
    updated = 0
    skipped = 0
    for path in paths:
        root = _read_kmz_kml(path)
        for placemark in _iter_placemarks(root, limit=limit):
            point = _extract_point(placemark)
            if not point:
                continue
            lon, lat = point
            name = placemark.properties.get("name") or placemark.name or "unnamed-closure"
            existing = db.query(FiberSpliceClosure).filter(FiberSpliceClosure.name == name).first()
            notes = _make_notes(placemark.properties)
            if existing:
                if not upsert:
                    skipped += 1
                    continue
                existing.name = name
                existing.latitude = lat
                existing.longitude = lon
                existing.geom = _point_geom(lon, lat)
                existing.notes = notes
                updated += 1
            else:
                closure = FiberSpliceClosure(
                    name=name,
                    latitude=lat,
                    longitude=lon,
                    geom=_point_geom(lon, lat),
                    notes=notes,
                )
                db.add(closure)
                created += 1
    return created, updated, skipped


def import_access_points(db, paths: list[Path], upsert: bool, limit: int | None):
    """Import fiber access points from KMZ files."""
    created = 0
    updated = 0
    skipped = 0
    for path in paths:
        root = _read_kmz_kml(path)
        for placemark in _iter_placemarks(root, limit=limit):
            point = _extract_point(placemark)
            if not point:
                continue
            lon, lat = point
            name = placemark.properties.get("Name") or placemark.name or "unnamed-ap"
            code = placemark.properties.get("access_pointid")
            existing = None
            if code:
                existing = db.query(FiberAccessPoint).filter(FiberAccessPoint.code == code).first()
            if not existing:
                existing = db.query(FiberAccessPoint).filter(FiberAccessPoint.name == name).first()
            notes = _make_notes(placemark.properties)
            ap_type = placemark.properties.get("Type")
            placement = placemark.properties.get("Placement")
            street = placemark.properties.get("Street")
            city = placemark.properties.get("City")
            county = placemark.properties.get("County")
            state = placemark.properties.get("State")
            if existing:
                if not upsert:
                    skipped += 1
                    continue
                existing.name = name
                existing.code = code
                existing.access_point_type = ap_type
                existing.placement = placement
                existing.latitude = lat
                existing.longitude = lon
                existing.geom = _point_geom(lon, lat)
                existing.street = street
                existing.city = city
                existing.county = county
                existing.state = state
                existing.notes = notes
                updated += 1
            else:
                ap = FiberAccessPoint(
                    name=name,
                    code=code,
                    access_point_type=ap_type,
                    placement=placement,
                    latitude=lat,
                    longitude=lon,
                    geom=_point_geom(lon, lat),
                    street=street,
                    city=city,
                    county=county,
                    state=state,
                    notes=notes,
                )
                db.add(ap)
                created += 1
    return created, updated, skipped


def import_wireless_masts(db, paths: list[Path], upsert: bool, limit: int | None):
    """Import wireless masts/poles from KMZ files."""
    created = 0
    updated = 0
    skipped = 0
    for path in paths:
        root = _read_kmz_kml(path)
        for placemark in _iter_placemarks(root, limit=limit):
            point = _extract_point(placemark)
            if not point:
                continue
            lon, lat = point
            name = placemark.properties.get("name") or placemark.name or "unnamed-mast"
            # Try to find existing by name
            existing = db.query(WirelessMast).filter(WirelessMast.name == name).first()
            notes = _make_notes(placemark.properties)
            structure_type = placemark.properties.get("poletypeid")
            status = placemark.properties.get("stage") or "active"
            if existing:
                if not upsert:
                    skipped += 1
                    continue
                existing.name = name
                existing.latitude = lat
                existing.longitude = lon
                existing.geom = _point_geom(lon, lat)
                existing.structure_type = structure_type
                existing.status = status
                existing.notes = notes
                updated += 1
            else:
                mast = WirelessMast(
                    name=name,
                    latitude=lat,
                    longitude=lon,
                    geom=_point_geom(lon, lat),
                    structure_type=structure_type,
                    status=status,
                    notes=notes,
                )
                db.add(mast)
                created += 1
    return created, updated, skipped


def import_buildings(db, paths: list[Path], upsert: bool, limit: int | None):
    """Import service buildings from KMZ files."""
    created = 0
    updated = 0
    skipped = 0
    for path in paths:
        root = _read_kmz_kml(path)
        for placemark in _iter_placemarks(root, limit=limit):
            point = _extract_point(placemark)
            if not point:
                continue
            lon, lat = point
            name = placemark.properties.get("Name") or placemark.name or "unnamed-building"
            code = placemark.properties.get("buildingid")
            existing = None
            if code:
                existing = db.query(ServiceBuilding).filter(ServiceBuilding.code == code).first()
            if not existing:
                existing = db.query(ServiceBuilding).filter(ServiceBuilding.name == name).first()
            notes = _make_notes(placemark.properties)
            clli = placemark.properties.get("CLLI")
            street = placemark.properties.get("Street")
            city = placemark.properties.get("City")
            state = placemark.properties.get("State")
            zip_code = placemark.properties.get("ZIP")
            work_order = placemark.properties.get("Work Order")
            # Handle polygon geometry for boundary
            boundary_geom = None
            if placemark.geometry_type == "Polygon" and len(placemark.coordinates) >= 3:
                coords = placemark.coordinates
                if coords[0] != coords[-1]:
                    coords = coords + [coords[0]]
                geojson = {"type": "Polygon", "coordinates": [coords]}
                boundary_geom = _geojson_to_geom(geojson)
            if existing:
                if not upsert:
                    skipped += 1
                    continue
                existing.name = name
                existing.code = code
                existing.clli = clli
                existing.latitude = lat
                existing.longitude = lon
                existing.geom = _point_geom(lon, lat)
                if boundary_geom is not None:
                    existing.boundary_geom = boundary_geom
                existing.street = street
                existing.city = city
                existing.state = state
                existing.zip_code = zip_code
                existing.work_order = work_order
                existing.notes = notes
                updated += 1
            else:
                building = ServiceBuilding(
                    name=name,
                    code=code,
                    clli=clli,
                    latitude=lat,
                    longitude=lon,
                    geom=_point_geom(lon, lat),
                    boundary_geom=boundary_geom,
                    street=street,
                    city=city,
                    state=state,
                    zip_code=zip_code,
                    work_order=work_order,
                    notes=notes,
                )
                db.add(building)
                created += 1
    return created, updated, skipped


def main():
    if load_dotenv is not None:
        load_dotenv()
    args = parse_args()
    db = SessionLocal()
    try:
        if args.purge:
            # Delete in dependency order: child tables before parent tables
            # Splitter hierarchy: PonPortSplitterLink/SplitterPortAssignment -> SplitterPort -> Splitter -> FdhCabinet
            db.query(PonPortSplitterLink).delete()
            db.query(SplitterPortAssignment).delete()
            db.query(SplitterPort).delete()
            db.query(Splitter).delete()
            db.query(FdhCabinet).delete()
            # Splice hierarchy: FiberSplice -> FiberSpliceTray -> FiberSpliceClosure
            db.query(FiberSplice).delete()
            db.query(FiberSpliceTray).delete()
            db.query(FiberSpliceClosure).delete()
            # Clear FK references before deleting segments
            db.query(AsBuiltRoute).filter(AsBuiltRoute.fiber_segment_id.isnot(None)).update(
                {AsBuiltRoute.fiber_segment_id: None}
            )
            db.query(FiberSegment).delete()
            # New tables (no dependencies)
            db.query(FiberAccessPoint).delete()
            db.query(WirelessMast).delete()
            db.query(ServiceBuilding).delete()

        cabinet_paths = [Path(p) for p in args.cabinet_kmz]
        splice_paths = [Path(p) for p in args.splice_kmz]
        segment_paths = [Path(p) for p in args.paths_kmz]
        access_point_paths = [Path(p) for p in args.access_point_kmz]
        mast_paths = [Path(p) for p in args.mast_kmz]
        building_paths = [Path(p) for p in args.building_kmz]

        total_created = total_updated = total_skipped = 0

        if cabinet_paths:
            created, updated, skipped = import_cabinets(db, cabinet_paths, args.upsert, args.limit)
            total_created += created
            total_updated += updated
            total_skipped += skipped

        if splice_paths:
            created, updated, skipped = import_splice_closures(db, splice_paths, args.upsert, args.limit)
            total_created += created
            total_updated += updated
            total_skipped += skipped

        if segment_paths:
            created, updated, skipped = import_segments(
                db,
                segment_paths,
                args.segment_type,
                args.cable_type,
                args.upsert,
                args.limit,
            )
            total_created += created
            total_updated += updated
            total_skipped += skipped

        if access_point_paths:
            created, updated, skipped = import_access_points(db, access_point_paths, args.upsert, args.limit)
            total_created += created
            total_updated += updated
            total_skipped += skipped

        if mast_paths:
            created, updated, skipped = import_wireless_masts(db, mast_paths, args.upsert, args.limit)
            total_created += created
            total_updated += updated
            total_skipped += skipped

        if building_paths:
            created, updated, skipped = import_buildings(db, building_paths, args.upsert, args.limit)
            total_created += created
            total_updated += updated
            total_skipped += skipped

        if args.dry_run:
            db.rollback()
            print("Dry run complete. No changes written.")
        else:
            db.commit()
            print("Import complete.")

        print(f"Created: {total_created} Updated: {total_updated} Skipped: {total_skipped}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
