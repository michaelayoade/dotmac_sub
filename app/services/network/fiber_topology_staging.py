"""Immutable KMZ source staging for the fiber-topology owner.

This module writes source facts and match suggestions only.  It never creates,
updates, merges, retires, or deletes canonical network/GIS assets.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from enum import StrEnum
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Protocol
from urllib.parse import SplitResult, urlsplit
from uuid import UUID

from defusedxml import ElementTree as ET
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.models.gis import ServiceBuilding
from app.models.network import (
    FdhCabinet,
    FiberAccessPoint,
    FiberSegment,
    FiberSpliceClosure,
)

KML_NS = {"kml": "http://www.opengis.net/kml/2.2"}
SOURCE_SYSTEM = "dotmac_osp_kmz"
NORMALIZATION_VERSION = 2
MAX_KML_BYTES = 100 * 1024 * 1024
MAX_KMZ_BYTES = 25 * 1024 * 1024
MAX_KMZ_ENTRIES = 64
MAX_KMZ_COMPRESSION_RATIO = 200
NIGERIA_LONGITUDE_RANGE = (2.0, 15.0)
NIGERIA_LATITUDE_RANGE = (4.0, 14.0)


class FiberAssetType(StrEnum):
    fiber_segment = "fiber_segment"
    fiber_access_point = "fiber_access_point"
    fdh_cabinet = "fdh_cabinet"
    splice_closure = "splice_closure"
    service_building = "service_building"
    support_structure = "support_structure"
    mixed_network_map = "mixed_network_map"
    unsupported = "unsupported"
    unclassified = "unclassified"


@dataclass(frozen=True)
class FiberSourceProfile:
    name: str
    default_filename: str
    asset_type: FiberAssetType
    external_id_key: str
    expected_geometry_type: str
    display_name_keys: tuple[str, ...]
    source_system: str = SOURCE_SYSTEM
    supported_asset_types: tuple[FiberAssetType, ...] = ()


SOURCE_PROFILES: dict[str, FiberSourceProfile] = {
    "osp_paths": FiberSourceProfile(
        name="osp_paths",
        default_filename="OSP Paths.kmz",
        asset_type=FiberAssetType.fiber_segment,
        external_id_key="spanid",
        expected_geometry_type="LineString",
        display_name_keys=("name", "spanid"),
    ),
    "osp_access_points": FiberSourceProfile(
        name="osp_access_points",
        default_filename="OSP Access point.kmz",
        asset_type=FiberAssetType.fiber_access_point,
        external_id_key="access_pointid",
        expected_geometry_type="Polygon",
        display_name_keys=("Name",),
    ),
    "osp_cabinets": FiberSourceProfile(
        name="osp_cabinets",
        default_filename="OSP Cabinet.kmz",
        asset_type=FiberAssetType.fdh_cabinet,
        external_id_key="fibermngrid",
        expected_geometry_type="Polygon",
        display_name_keys=("name",),
    ),
    "osp_splice_info": FiberSourceProfile(
        name="osp_splice_info",
        default_filename="OSP Splice info.kmz",
        asset_type=FiberAssetType.splice_closure,
        external_id_key="enclosureid",
        expected_geometry_type="Polygon",
        display_name_keys=("name",),
    ),
    "osp_buildings": FiberSourceProfile(
        name="osp_buildings",
        default_filename="OSP Building.kmz",
        asset_type=FiberAssetType.service_building,
        external_id_key="buildingid",
        expected_geometry_type="Polygon",
        display_name_keys=("Name",),
    ),
    "osp_air_fiber": FiberSourceProfile(
        name="osp_air_fiber",
        default_filename="OSP Air fiber.kmz",
        asset_type=FiberAssetType.support_structure,
        external_id_key="poleid",
        expected_geometry_type="Point",
        display_name_keys=("name",),
    ),
    "mixed_network_map": FiberSourceProfile(
        name="mixed_network_map",
        default_filename="network-map.kmz",
        asset_type=FiberAssetType.mixed_network_map,
        external_id_key="dotmac_asset_id",
        expected_geometry_type="per_asset_type",
        display_name_keys=("name", "code", "display_name"),
        supported_asset_types=(
            FiberAssetType.fiber_segment,
            FiberAssetType.fiber_access_point,
            FiberAssetType.fdh_cabinet,
            FiberAssetType.splice_closure,
            FiberAssetType.service_building,
            FiberAssetType.support_structure,
        ),
    ),
    "crm_fdh_cabinets": FiberSourceProfile(
        name="crm_fdh_cabinets",
        default_filename="crm_fdh_cabinets.kml",
        asset_type=FiberAssetType.fdh_cabinet,
        external_id_key="crm_id",
        expected_geometry_type="Point",
        display_name_keys=("name", "code", "crm_id"),
        source_system="dotmac_crm_fiber_map",
    ),
    "crm_access_points": FiberSourceProfile(
        name="crm_access_points",
        default_filename="crm_access_points.kml",
        asset_type=FiberAssetType.fiber_access_point,
        external_id_key="crm_id",
        expected_geometry_type="Point",
        display_name_keys=("name", "code", "crm_id"),
        source_system="dotmac_crm_fiber_map",
    ),
    "crm_splice_closures": FiberSourceProfile(
        name="crm_splice_closures",
        default_filename="crm_splice_closures.kml",
        asset_type=FiberAssetType.splice_closure,
        external_id_key="crm_id",
        expected_geometry_type="Point",
        display_name_keys=("name", "crm_id"),
        source_system="dotmac_crm_fiber_map",
    ),
    "crm_fiber_segments": FiberSourceProfile(
        name="crm_fiber_segments",
        default_filename="crm_fiber_segments.kml",
        asset_type=FiberAssetType.fiber_segment,
        external_id_key="crm_id",
        expected_geometry_type="LineString",
        display_name_keys=("name", "crm_id"),
        source_system="dotmac_crm_fiber_map",
    ),
    "crm_service_buildings": FiberSourceProfile(
        name="crm_service_buildings",
        default_filename="crm_service_buildings.kml",
        asset_type=FiberAssetType.service_building,
        external_id_key="crm_id",
        expected_geometry_type="Point",
        display_name_keys=("name", "code", "crm_id"),
        source_system="dotmac_crm_fiber_map",
    ),
}

_MIXED_TYPE_ALIASES: dict[str, FiberAssetType] = {
    "fibersegment": FiberAssetType.fiber_segment,
    "accesspoint": FiberAssetType.fiber_access_point,
    "fiberaccesspoint": FiberAssetType.fiber_access_point,
    "fdhcabinet": FiberAssetType.fdh_cabinet,
    "spliceclosure": FiberAssetType.splice_closure,
    "servicebuilding": FiberAssetType.service_building,
    "supportstructure": FiberAssetType.support_structure,
}
_MIXED_TYPE_ID_KEYS: dict[FiberAssetType, tuple[str, ...]] = {
    FiberAssetType.fiber_segment: ("spanid",),
    FiberAssetType.fiber_access_point: ("access_pointid", "accesspointid"),
    FiberAssetType.fdh_cabinet: ("fibermngrid",),
    FiberAssetType.splice_closure: ("enclosureid",),
    FiberAssetType.service_building: ("buildingid",),
    FiberAssetType.support_structure: ("poleid",),
}
_MIXED_GEOMETRY_TYPES: dict[FiberAssetType, frozenset[str]] = {
    FiberAssetType.fiber_segment: frozenset({"LineString"}),
    FiberAssetType.support_structure: frozenset({"Point"}),
    FiberAssetType.fiber_access_point: frozenset({"Point", "Polygon"}),
    FiberAssetType.fdh_cabinet: frozenset({"Point", "Polygon"}),
    FiberAssetType.splice_closure: frozenset({"Point", "Polygon"}),
    FiberAssetType.service_building: frozenset({"Point", "Polygon"}),
}


def mixed_geometry_compatible(asset_type: FiberAssetType, geometry_type: str) -> bool:
    """Return whether a mixed-profile feature may use this geometry."""

    return geometry_type in _MIXED_GEOMETRY_TYPES.get(asset_type, frozenset())


_MIXED_SAFE_PROPERTY_KEYS = frozenset(
    {
        "dotmac_asset_type",
        "asset_type",
        "feature_type",
        "type",
        "dotmac_asset_id",
        "spanid",
        "access_pointid",
        "fibermngrid",
        "enclosureid",
        "buildingid",
        "poleid",
        "external_id",
        "id",
        "name",
        "code",
        "display_name",
        "description",
        "kml_placemark_id",
        "icon_href",
        "icon_color",
        "icon_scale",
        "line_color",
        "line_width",
        "polygon_color",
        "resource_warnings",
    }
)


@dataclass(frozen=True)
class ParsedFiberFeature:
    row_number: int
    asset_type: FiberAssetType
    external_id: str | None
    display_name: str | None
    geometry_type: str
    geometry_geojson: dict
    source_properties: dict
    content_sha256: str
    geometry_sha256: str
    blocker_codes: tuple[str, ...]
    suggested_asset_type: FiberAssetType | None = None


@dataclass(frozen=True)
class FiberFeatureMatchPlan:
    feature: ParsedFiberFeature
    match_status: str
    match_reasons: tuple[str, ...]
    candidate_asset_ids: tuple[str, ...]
    canonical_asset_type: str | None
    canonical_asset_id: object | None
    prior_feature_id: object | None

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["canonical_asset_id"] = (
            str(self.canonical_asset_id) if self.canonical_asset_id else None
        )
        payload["prior_feature_id"] = (
            str(self.prior_feature_id) if self.prior_feature_id else None
        )
        return payload


@dataclass(frozen=True)
class FiberSourcePreview:
    source_system: str
    profile: FiberSourceProfile
    source_name: str
    file_sha256: str
    manifest_sha256: str
    features: tuple[FiberFeatureMatchPlan, ...]
    status_counts: dict[str, int]
    kml_entry_name: str

    @property
    def feature_count(self) -> int:
        return len(self.features)

    @property
    def blocker_count(self) -> int:
        return self.status_counts.get("blocked", 0)

    @property
    def candidate_count(self) -> int:
        return sum(
            self.status_counts.get(status, 0)
            for status in ("exact_external", "candidate", "ambiguous")
        )

    def to_dict(self, *, include_features: bool = False) -> dict:
        payload = {
            "source_system": self.source_system,
            "profile": self.profile.name,
            "source_name": self.source_name,
            "asset_type": self.profile.asset_type,
            "external_id_key": self.profile.external_id_key,
            "expected_geometry_type": self.profile.expected_geometry_type,
            "normalization_version": NORMALIZATION_VERSION,
            "file_sha256": self.file_sha256,
            "manifest_sha256": self.manifest_sha256,
            "feature_count": self.feature_count,
            "blocker_count": self.blocker_count,
            "candidate_count": self.candidate_count,
            "status_counts": dict(sorted(self.status_counts.items())),
            "kml_entry_name": self.kml_entry_name,
        }
        if include_features:
            payload["features"] = [feature.to_dict() for feature in self.features]
        return payload


@dataclass(frozen=True)
class FiberSourceStageResult:
    batch_id: object
    created: bool
    status: str
    feature_count: int
    blocker_count: int
    candidate_count: int
    new_count: int
    unchanged_count: int
    file_sha256: str
    manifest_sha256: str

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["batch_id"] = str(self.batch_id)
        return payload


@dataclass(frozen=True)
class _CanonicalLookup:
    exact_external: dict[str, tuple[object, ...]]
    by_name: dict[str, tuple[object, ...]]


class _CanonicalIdRow(Protocol):
    id: UUID


def source_profile(name: str) -> FiberSourceProfile:
    try:
        return SOURCE_PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"Unsupported fiber source profile: {name}") from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256_json(value) -> str:
    return _sha256_bytes(_canonical_json(value))


def _normalized_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def _read_kml_bytes(raw: bytes, source_name: str) -> tuple[bytes, bytes, str]:
    suffix = Path(source_name).suffix.casefold()
    if suffix == ".kml":
        if len(raw) > MAX_KML_BYTES:
            raise ValueError(
                "KML document is larger than the supported 100 MB limit. "
                "Reduce its size or split the map into smaller files."
            )
        return raw, raw, Path(source_name).name
    if suffix != ".kmz":
        raise ValueError(
            "Choose a .kml or .kmz file. Other map formats are not supported."
        )
    if len(raw) > MAX_KMZ_BYTES:
        raise ValueError(
            "KMZ archive is larger than the supported 25 MB upload limit. "
            "Reduce its size or export fewer map layers."
        )

    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            entries_all = archive.infolist()
            if len(entries_all) > MAX_KMZ_ENTRIES:
                raise ValueError(
                    "KMZ archive contains more than 64 files. Re-export it with "
                    "one KML document and only the resources it needs."
                )
            entries = [
                info
                for info in entries_all
                if not info.is_dir() and info.filename.casefold().endswith(".kml")
            ]
            if len(entries) != 1:
                raise ValueError(
                    "KMZ archive must contain exactly one .kml document. "
                    "Choose a KML file or re-export the KMZ with one map document."
                )
            entry = entries[0]
            if entry.file_size > MAX_KML_BYTES:
                raise ValueError(
                    "KML document inside the KMZ exceeds 100 MB. Split the map "
                    "into smaller files and export again."
                )
            if entry.compress_size == 0 and entry.file_size > 0:
                raise ValueError(
                    "KMZ KML entry has invalid compressed-size metadata. "
                    "Re-export the archive from the map application."
                )
            if (
                entry.compress_size > 0
                and entry.file_size / entry.compress_size > MAX_KMZ_COMPRESSION_RATIO
            ):
                raise ValueError(
                    "KMZ KML document expands beyond the permitted compression "
                    "ratio. Re-export it with normal ZIP compression."
                )
            return raw, archive.read(entry), entry.filename
    except zipfile.BadZipFile as exc:
        raise ValueError(
            "KMZ archive is not a valid ZIP file. Re-export the map as KML or KMZ."
        ) from exc


def _read_kml(path: Path) -> tuple[bytes, bytes, str]:
    return _read_kml_bytes(path.read_bytes(), path.name)


def _properties(placemark: ET.Element) -> dict[str, str | None]:
    properties: dict[str, str | None] = {}
    for element in placemark.findall(".//kml:SimpleData", KML_NS):
        key = (element.attrib.get("name") or "").strip()
        if key:
            value = (element.text or "").strip()
            properties[key] = value or None
    for element in placemark.findall(".//kml:Data", KML_NS):
        key = (element.attrib.get("name") or "").strip()
        if key:
            value = element.findtext("kml:value", default="", namespaces=KML_NS).strip()
            properties[key] = value or None
    return dict(sorted(properties.items(), key=lambda item: item[0].casefold()))


def _property(properties: dict[str, str | None], key: str) -> str | None:
    target = key.casefold()
    for candidate, value in properties.items():
        if candidate.casefold() == target:
            return (value or "").strip() or None
    return None


def _coordinates(text: str) -> tuple[list[list[float]], list[str]]:
    coordinates: list[list[float]] = []
    blockers: list[str] = []
    for token in text.split():
        parts = token.split(",")
        if len(parts) < 2:
            blockers.append("invalid_coordinate")
            continue
        try:
            longitude = round(float(parts[0]), 7)
            latitude = round(float(parts[1]), 7)
        except ValueError:
            blockers.append("invalid_coordinate")
            continue
        if not math.isfinite(longitude) or not math.isfinite(latitude):
            blockers.append("invalid_coordinate")
            continue
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            blockers.append("invalid_coordinate")
        if not (
            NIGERIA_LONGITUDE_RANGE[0] <= longitude <= NIGERIA_LONGITUDE_RANGE[1]
            and NIGERIA_LATITUDE_RANGE[0] <= latitude <= NIGERIA_LATITUDE_RANGE[1]
        ):
            blockers.append("coordinate_outside_nigeria")
        coordinates.append([longitude, latitude])
    return coordinates, list(dict.fromkeys(blockers))


class _PlainTextDescription(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if value:
            self.parts.append(value)


def _description(placemark: ET.Element) -> str | None:
    raw = placemark.findtext("kml:description", default="", namespaces=KML_NS)
    if not raw:
        return None
    parser = _PlainTextDescription()
    parser.feed(raw[:16_384])
    normalized = " ".join(" ".join(parser.parts).split())
    return normalized[:4_000] or None


def _suggested_asset_type(
    geometry_type: str,
    placemark_name: str | None,
    description: str | None,
) -> FiberAssetType | None:
    if geometry_type == "LineString":
        return FiberAssetType.fiber_segment
    label = _normalized_key(f"{placemark_name or ''} {description or ''}")
    hints: tuple[tuple[FiberAssetType, tuple[str, ...]], ...] = (
        (FiberAssetType.fdh_cabinet, ("fdhcabinet", "cabinet")),
        (
            FiberAssetType.fiber_access_point,
            ("fiberaccesspoint", "accesspoint", "fat", "fap"),
        ),
        (FiberAssetType.splice_closure, ("spliceclosure", "closure")),
        (FiberAssetType.service_building, ("servicebuilding",)),
        (
            FiberAssetType.support_structure,
            ("supportstructure", "supportpole", "pole"),
        ),
    )
    matches = {
        asset_type
        for asset_type, terms in hints
        if any(term in label for term in terms)
    }
    if len(matches) == 1:
        suggestion = next(iter(matches))
        if mixed_geometry_compatible(suggestion, geometry_type):
            return suggestion
    return None


def _external_icon_url(value: str) -> tuple[str | None, str | None]:
    """Classify an HTTPS reference without DNS lookup or network access."""

    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (
            parsed.scheme.casefold() != "https"
            or not host
            or any(character.isspace() for character in host)
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None, "icon_reference_not_https"
        port = parsed.port
        if port is not None and not 1 <= port <= 65_535:
            return None, "icon_reference_malformed"
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            normalized_host = host.casefold().rstrip(".")
            if normalized_host in {
                "localhost",
                "local",
                "internal",
                "intranet",
            } or normalized_host.endswith(
                (".localhost", ".local", ".internal", ".lan", ".home")
            ):
                return None, "icon_reference_internal_host"
        else:
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_reserved
                or address.is_unspecified
            ):
                return None, "icon_reference_internal_address"
        return value[:2_048], None
    except ValueError:
        return None, "icon_reference_malformed"


def _geometry(placemark: ET.Element) -> tuple[str, dict, tuple[str, ...]]:
    parsed: list[tuple[str, dict, list[str]]] = []
    for geometry_type in ("Point", "LineString", "Polygon"):
        for element in placemark.findall(f".//kml:{geometry_type}", KML_NS):
            blockers: list[str] = []
            geojson: dict[str, object]
            if geometry_type == "Polygon":
                rings = element.findall(".//kml:LinearRing/kml:coordinates", KML_NS)
                if not rings:
                    blockers.append("missing_polygon_coordinates")
                parsed_rings: list[list[list[float]]] = []
                for ring in rings:
                    coordinates, ring_blockers = _coordinates(ring.text or "")
                    blockers.extend(ring_blockers)
                    if coordinates and coordinates[0] != coordinates[-1]:
                        coordinates.append(coordinates[0])
                    if len(coordinates) < 4:
                        blockers.append("invalid_polygon_geometry")
                    parsed_rings.append(coordinates)
                geojson = {"type": "Polygon", "coordinates": parsed_rings}
            else:
                text = element.findtext(
                    "kml:coordinates", default="", namespaces=KML_NS
                )
                coordinates, blockers = _coordinates(text)
                if geometry_type == "Point":
                    if len(coordinates) != 1:
                        blockers.append("invalid_point_geometry")
                    geojson = {
                        "type": "Point",
                        "coordinates": coordinates[0] if coordinates else [],
                    }
                else:
                    if len(coordinates) < 2:
                        blockers.append("invalid_linestring_geometry")
                    geojson = {"type": "LineString", "coordinates": coordinates}
            parsed.append((geometry_type, geojson, blockers))
    if not parsed:
        return (
            "Unknown",
            {"type": "GeometryCollection", "geometries": []},
            ("missing_supported_geometry",),
        )
    if len(parsed) > 1:
        return (
            "GeometryCollection",
            {"type": "GeometryCollection", "geometries": [item[1] for item in parsed]},
            tuple(
                dict.fromkeys(
                    [
                        "multiple_geometry_components",
                        *[code for item in parsed for code in item[2]],
                    ]
                )
            ),
        )
    geometry_type, geojson, blockers = parsed[0]
    return geometry_type, geojson, tuple(dict.fromkeys(blockers))


def _mixed_asset_type(
    properties: dict[str, str | None],
) -> tuple[FiberAssetType, str | None]:
    declared = next(
        (
            value
            for key in ("dotmac_asset_type", "asset_type", "feature_type", "type")
            if (value := _property(properties, key))
        ),
        None,
    )
    if declared:
        normalized = _normalized_key(declared)
        return _MIXED_TYPE_ALIASES.get(normalized, FiberAssetType.unsupported), declared
    for asset_type, keys in _MIXED_TYPE_ID_KEYS.items():
        if any(_property(properties, key) for key in keys):
            return asset_type, None
    return FiberAssetType.unclassified, None


def _style_properties(
    root: ET.Element, placemark: ET.Element
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Read local KML style values and record HTTPS icon references without fetching."""

    style = placemark.find("kml:Style", KML_NS)
    style_url = placemark.findtext("kml:styleUrl", default="", namespaces=KML_NS)
    if style is None and style_url.startswith("#"):
        style_id = style_url[1:]
        style = next(
            (
                candidate
                for candidate in root.findall(".//kml:Style", KML_NS)
                if candidate.attrib.get("id") == style_id
            ),
            None,
        )
        if style is None:
            style_map = next(
                (
                    candidate
                    for candidate in root.findall(".//kml:StyleMap", KML_NS)
                    if candidate.attrib.get("id") == style_id
                ),
                None,
            )
            if style_map is not None:
                normal = next(
                    (
                        pair.findtext("kml:styleUrl", default="", namespaces=KML_NS)
                        for pair in style_map.findall("kml:Pair", KML_NS)
                        if pair.findtext("kml:key", default="", namespaces=KML_NS)
                        == "normal"
                    ),
                    "",
                )
                if normal.startswith("#"):
                    normal_id = normal[1:]
                    style = next(
                        (
                            candidate
                            for candidate in root.findall(".//kml:Style", KML_NS)
                            if candidate.attrib.get("id") == normal_id
                        ),
                        None,
                    )
    values: dict[str, str] = {}
    warnings: list[str] = []
    if style is not None:
        for source_name, target_name in (
            ("kml:IconStyle/kml:color", "icon_color"),
            ("kml:IconStyle/kml:scale", "icon_scale"),
            ("kml:LineStyle/kml:color", "line_color"),
            ("kml:LineStyle/kml:width", "line_width"),
            ("kml:PolyStyle/kml:color", "polygon_color"),
        ):
            value = style.findtext(source_name, default="", namespaces=KML_NS).strip()
            if value:
                values[target_name] = value[:80]
        href = style.findtext(
            "kml:IconStyle/kml:Icon/kml:href", default="", namespaces=KML_NS
        ).strip()
    else:
        href = ""
    inline = placemark.find("kml:Style/kml:IconStyle/kml:Icon/kml:href", KML_NS)
    if inline is not None and (inline.text or "").strip():
        href = (inline.text or "").strip()
    if href:
        safe_href, warning_code = _external_icon_url(href)
        if safe_href:
            # This is metadata only. The server and preview do not fetch it.
            values["icon_href"] = safe_href
            parsed = urlsplit(safe_href)
            warnings.append(
                f"remote_icon_not_checked:{parsed.hostname or ''}{parsed.path[:160]}"
            )
        else:
            try:
                parsed = urlsplit(href)
            except ValueError:
                parsed = SplitResult("", "", href[:200], "", "")
            resource = f"{parsed.scheme or 'relative'}:{parsed.hostname or ''}{parsed.path[:160]}"
            warnings.append(f"{warning_code or 'icon_not_loaded'}:{resource}")
    if style_url and not style_url.startswith("#"):
        try:
            parsed_style = urlsplit(style_url)
        except ValueError:
            parsed_style = SplitResult("", "", style_url[:200], "", "")
        warnings.append(
            f"external_style_not_loaded:{parsed_style.scheme or 'relative'}:"
            f"{parsed_style.path[:200]}"
        )
    if warnings:
        values["resource_warnings"] = "|".join(dict.fromkeys(warnings))
    return values, tuple(dict.fromkeys(warnings))


def _mixed_external_id(
    properties: dict[str, str | None],
    *,
    asset_type: FiberAssetType,
    placemark: ET.Element,
) -> str | None:
    keys = (
        "dotmac_asset_id",
        *_MIXED_TYPE_ID_KEYS.get(asset_type, ()),
        "external_id",
        "id",
    )
    for key in keys:
        value = _property(properties, key)
        if value:
            return value
    return (placemark.attrib.get("id") or "").strip() or None


def _parse_features(
    kml: bytes, profile: FiberSourceProfile
) -> list[ParsedFiberFeature]:
    try:
        root = ET.fromstring(kml)
    except ET.ParseError as exc:
        line, column = exc.position
        raise ValueError(
            f"KML is malformed near line {line}, column {column}. Check the file "
            "is a complete KML document and try exporting it again."
        ) from exc
    network_link = root.find(".//kml:NetworkLink", KML_NS)
    network_link_name: str | None = None
    if network_link is not None:
        network_link_name = (
            network_link.findtext(
                "kml:name", default="NetworkLink", namespaces=KML_NS
            ).strip()
            or "NetworkLink"
        )
        if not root.findall(".//kml:Placemark", KML_NS):
            name = network_link_name
            raise ValueError(
                f"KML document link '{name}' is not expanded. Download the linked "
                "KML separately and upload it directly."
            )

    parsed: list[ParsedFiberFeature] = []
    for row_number, placemark in enumerate(
        root.findall(".//kml:Placemark", KML_NS), start=1
    ):
        properties = _properties(placemark)
        suggested_asset_type: FiberAssetType | None = None
        if profile.name == "mixed_network_map":
            asset_type, declared_asset_type = _mixed_asset_type(properties)
            placemark_name = (
                placemark.findtext("kml:name", default="", namespaces=KML_NS).strip()
                or None
            )
            geometry_type, geojson, geometry_blockers = _geometry(placemark)
            if asset_type is FiberAssetType.unclassified:
                suggested_asset_type = _suggested_asset_type(
                    geometry_type,
                    placemark_name,
                    _description(placemark),
                )
            external_id = _mixed_external_id(
                properties, asset_type=asset_type, placemark=placemark
            )
            properties = {
                key: value
                for key, value in properties.items()
                if key.casefold() in _MIXED_SAFE_PROPERTY_KEYS
            }
        else:
            asset_type = profile.asset_type
            declared_asset_type = None
            placemark_name = (
                placemark.findtext("kml:name", default="", namespaces=KML_NS).strip()
                or None
            )
            external_id = _property(properties, profile.external_id_key)
            if not external_id:
                external_id = (placemark.attrib.get("id") or "").strip() or None
            geometry_type, geojson, geometry_blockers = _geometry(placemark)
        description = _description(placemark)
        styles, _resource_warnings = _style_properties(root, placemark)
        if network_link_name is not None:
            _resource_warnings = (
                *_resource_warnings,
                f"network_link_not_expanded:{network_link_name[:160]}",
            )
            styles["resource_warnings"] = "|".join(dict.fromkeys(_resource_warnings))
        display_name = placemark_name or next(
            (
                value
                for key in profile.display_name_keys
                if (value := _property(properties, key))
            ),
            None,
        )
        blockers = list(geometry_blockers)
        if asset_type is FiberAssetType.unclassified:
            blockers.append("missing_asset_type")
        elif asset_type is FiberAssetType.unsupported:
            blockers.append("unsupported_asset_type")
        if profile.name == "mixed_network_map":
            if asset_type is FiberAssetType.unsupported:
                external_id = None
                properties = {}
            if asset_type is not FiberAssetType.unsupported and placemark.attrib.get(
                "id"
            ):
                properties["kml_placemark_id"] = placemark.attrib["id"][:255]
            if description:
                properties["description"] = description
            properties.update(styles)
            if suggested_asset_type is not None:
                properties["suggested_asset_type"] = suggested_asset_type.value
        if not external_id and profile.name != "mixed_network_map":
            blockers.append("missing_external_id")
        if profile.name == "mixed_network_map" and asset_type not in {
            FiberAssetType.unclassified,
            FiberAssetType.unsupported,
        }:
            if not mixed_geometry_compatible(asset_type, geometry_type):
                blockers.append("unexpected_geometry_type")
        elif (
            profile.name != "mixed_network_map"
            and geometry_type != profile.expected_geometry_type
        ):
            blockers.append("unexpected_geometry_type")
        geometry_sha256 = _sha256_json(geojson)
        normalized = {
            "normalization_version": NORMALIZATION_VERSION,
            "asset_type": asset_type,
            "declared_asset_type": declared_asset_type,
            "external_id": external_id,
            "display_name": display_name,
            "description": description,
            "geometry": geojson,
            "properties": properties,
        }
        parsed.append(
            ParsedFiberFeature(
                row_number=row_number,
                asset_type=asset_type,
                external_id=external_id,
                display_name=display_name,
                geometry_type=geometry_type,
                geometry_geojson=geojson,
                source_properties=properties,
                content_sha256=_sha256_json(normalized),
                geometry_sha256=geometry_sha256,
                blocker_codes=tuple(dict.fromkeys(blockers)),
                suggested_asset_type=suggested_asset_type,
            )
        )
    if not parsed:
        raise ValueError("KML source contains no placemarks")
    return parsed


def _ids_by_key(rows, attribute: str) -> dict[str, tuple[object, ...]]:
    grouped: dict[str, list[object]] = defaultdict(list)
    for row in rows:
        key = _normalized_key(getattr(row, attribute, None))
        if key:
            grouped[key].append(row.id)
    return {key: tuple(values) for key, values in grouped.items()}


def _ids_by_record_id(rows: Iterable[_CanonicalIdRow]) -> dict[str, tuple[object, ...]]:
    grouped: dict[str, list[object]] = defaultdict(list)
    for row in rows:
        key = _normalized_key(str(row.id))
        if key:
            grouped[key].append(row.id)
    return {key: tuple(values) for key, values in grouped.items()}


def _mixed_exact_external(
    rows: Iterable[_CanonicalIdRow], external_key: str
) -> dict[str, tuple[object, ...]]:
    matches = _ids_by_key(rows, external_key)
    for key, identifiers in _ids_by_record_id(rows).items():
        matches[key] = (*matches.get(key, ()), *identifiers)
    return matches


def _canonical_lookup(db: Session, profile: FiberSourceProfile) -> _CanonicalLookup:
    if profile.asset_type == "fdh_cabinet":
        fdh_rows = db.scalars(select(FdhCabinet)).all()
        return _CanonicalLookup(
            (
                _mixed_exact_external(fdh_rows, "code")
                if profile.name == "mixed_network_map"
                else _ids_by_key(fdh_rows, "code")
            ),
            _ids_by_key(fdh_rows, "name"),
        )
    if profile.asset_type == "fiber_access_point":
        access_point_rows = db.scalars(select(FiberAccessPoint)).all()
        return _CanonicalLookup(
            (
                _mixed_exact_external(access_point_rows, "code")
                if profile.name == "mixed_network_map"
                else _ids_by_key(access_point_rows, "code")
            ),
            _ids_by_key(access_point_rows, "name"),
        )
    if profile.asset_type == "service_building":
        building_rows = db.scalars(select(ServiceBuilding)).all()
        return _CanonicalLookup(
            (
                _mixed_exact_external(building_rows, "code")
                if profile.name == "mixed_network_map"
                else _ids_by_key(building_rows, "code")
            ),
            _ids_by_key(building_rows, "name"),
        )
    if profile.asset_type == "fiber_segment":
        segment_rows = db.scalars(select(FiberSegment)).all()
        return _CanonicalLookup(
            _ids_by_record_id(segment_rows)
            if profile.name == "mixed_network_map"
            else {},
            _ids_by_key(segment_rows, "name"),
        )
    if profile.asset_type == "splice_closure":
        closure_rows = db.scalars(select(FiberSpliceClosure)).all()
        return _CanonicalLookup(
            _ids_by_record_id(closure_rows)
            if profile.name == "mixed_network_map"
            else {},
            _ids_by_key(closure_rows, "name"),
        )
    return _CanonicalLookup({}, {})


def _prior_features(
    db: Session, profile: FiberSourceProfile
) -> dict[tuple[str, str], FiberTopologyStagedFeature]:
    asset_types = profile.supported_asset_types or (profile.asset_type,)
    rows = db.scalars(
        select(FiberTopologyStagedFeature)
        .join(
            FiberTopologySourceBatch,
            FiberTopologySourceBatch.id == FiberTopologyStagedFeature.batch_id,
        )
        .where(
            FiberTopologySourceBatch.source_system == profile.source_system,
            FiberTopologySourceBatch.profile == profile.name,
            FiberTopologyStagedFeature.asset_type.in_(asset_types),
            FiberTopologyStagedFeature.external_id.is_not(None),
        )
        .order_by(
            FiberTopologySourceBatch.created_at.desc(),
            FiberTopologyStagedFeature.created_at.desc(),
        )
    ).all()
    result: dict[tuple[str, str], FiberTopologyStagedFeature] = {}
    for row in rows:
        key = _normalized_key(row.external_id)
        identity = (row.asset_type, key)
        if key and identity not in result:
            result[identity] = row
    return result


def _plan_features(
    db: Session,
    profile: FiberSourceProfile,
    features: list[ParsedFiberFeature],
) -> tuple[FiberFeatureMatchPlan, ...]:
    canonical_by_type: dict[str, _CanonicalLookup] = {}
    prior = _prior_features(db, profile)
    external_counts = Counter(
        (feature.asset_type, key)
        for feature in features
        if (key := _normalized_key(feature.external_id))
    )
    name_counts = Counter(
        (feature.asset_type, key)
        for feature in features
        if (key := _normalized_key(feature.display_name))
    )
    geometry_counts = Counter(
        (feature.asset_type, feature.geometry_sha256) for feature in features
    )

    plans: list[FiberFeatureMatchPlan] = []
    for feature in features:
        blockers = list(feature.blocker_codes)
        reasons: list[str] = []
        candidates: set[object] = set()
        canonical_id = None
        prior_id = None
        canonical = canonical_by_type.get(feature.asset_type)
        if canonical is None:
            canonical = _canonical_lookup(
                db,
                FiberSourceProfile(
                    name=profile.name,
                    default_filename=profile.default_filename,
                    asset_type=feature.asset_type,
                    external_id_key=profile.external_id_key,
                    expected_geometry_type=profile.expected_geometry_type,
                    display_name_keys=profile.display_name_keys,
                    source_system=profile.source_system,
                ),
            )
            canonical_by_type[feature.asset_type] = canonical
        external_key = _normalized_key(feature.external_id)
        name_key = _normalized_key(feature.display_name)

        if external_key and external_counts[(feature.asset_type, external_key)] > 1:
            if profile.name == "mixed_network_map":
                reasons.append("duplicate_source_external_id")
            else:
                blockers.append("duplicate_external_id")
        if name_key and name_counts[(feature.asset_type, name_key)] > 1:
            reasons.append("duplicate_source_name")
        if geometry_counts[(feature.asset_type, feature.geometry_sha256)] > 1:
            reasons.append("duplicate_source_geometry")
        if not feature.display_name:
            reasons.append("missing_display_name")

        prior_feature = (
            prior.get((feature.asset_type, external_key)) if external_key else None
        )
        if prior_feature is not None:
            prior_id = prior_feature.id
            if prior_feature.content_sha256 == feature.content_sha256:
                reasons.append("unchanged_source_identity")
            else:
                reasons.append("changed_source_identity")

        exact = canonical.exact_external.get(external_key, ())
        if exact:
            candidates.update(exact)
            reasons.append("canonical_external_id_match")
        name_matches = canonical.by_name.get(name_key, ()) if name_key else ()
        if name_matches:
            candidates.update(name_matches)
            reasons.append("canonical_normalized_name_match")

        if blockers:
            status = "blocked"
        elif len(exact) > 1 or len(name_matches) > 1:
            status = "ambiguous"
        elif "changed_source_identity" in reasons:
            status = "candidate"
        elif exact:
            status = "exact_external"
            canonical_id = exact[0]
        elif name_matches or any(
            reason
            in {
                "duplicate_source_name",
                "duplicate_source_geometry",
                "duplicate_source_external_id",
                "missing_display_name",
            }
            for reason in reasons
        ):
            status = "candidate"
            if len(name_matches) == 1:
                canonical_id = name_matches[0]
        elif "unchanged_source_identity" in reasons:
            status = "unchanged"
        else:
            status = "new"

        plans.append(
            FiberFeatureMatchPlan(
                feature=ParsedFiberFeature(
                    **{
                        **asdict(feature),
                        "blocker_codes": tuple(dict.fromkeys(blockers)),
                    }
                ),
                match_status=status,
                match_reasons=tuple(dict.fromkeys(reasons)),
                candidate_asset_ids=tuple(sorted(str(value) for value in candidates)),
                canonical_asset_type=(feature.asset_type if canonical_id else None),
                canonical_asset_id=canonical_id,
                prior_feature_id=prior_id,
            )
        )
    return tuple(plans)


def revalidate_mixed_features(
    db: Session,
    features: tuple[ParsedFiberFeature, ...],
) -> tuple[FiberFeatureMatchPlan, ...]:
    """Recompute matching and blockers for effective reviewed feature values."""

    return _plan_features(db, SOURCE_PROFILES["mixed_network_map"], list(features))


def preview_fiber_source(
    db: Session, path: str | Path, profile_name: str
) -> FiberSourcePreview:
    """Parse and plan one source without persisting anything."""
    profile = source_profile(profile_name)
    source_path = Path(path)
    raw, kml, kml_entry_name = _read_kml(source_path)
    parsed = _parse_features(kml, profile)
    manifest_rows = sorted(
        [
            {
                "external_id": feature.external_id,
                "content_sha256": feature.content_sha256,
            }
            for feature in parsed
        ],
        key=lambda row: (
            _normalized_key(row["external_id"]),
            row["content_sha256"],
        ),
    )
    plans = _plan_features(db, profile, parsed)
    return FiberSourcePreview(
        source_system=profile.source_system,
        profile=profile,
        source_name=source_path.name,
        file_sha256=_sha256_bytes(raw),
        manifest_sha256=_sha256_json(manifest_rows),
        features=plans,
        status_counts=dict(Counter(plan.match_status for plan in plans)),
        kml_entry_name=kml_entry_name,
    )


def preview_uploaded_fiber_source(
    db: Session,
    *,
    content: bytes,
    source_name: str,
    profile_name: str,
) -> FiberSourcePreview:
    """Parse uploaded source bytes without trusting or materializing archive paths."""

    profile = source_profile(profile_name)
    raw, kml, kml_entry_name = _read_kml_bytes(content, source_name)
    parsed = _parse_features(kml, profile)
    manifest_rows = sorted(
        [
            {
                "external_id": feature.external_id,
                "content_sha256": feature.content_sha256,
            }
            for feature in parsed
        ],
        key=lambda row: (
            _normalized_key(row["external_id"]),
            row["content_sha256"],
        ),
    )
    plans = _plan_features(db, profile, parsed)
    return FiberSourcePreview(
        source_system=profile.source_system,
        profile=profile,
        source_name=Path(source_name).name,
        file_sha256=_sha256_bytes(raw),
        manifest_sha256=_sha256_json(manifest_rows),
        features=plans,
        status_counts=dict(Counter(plan.match_status for plan in plans)),
        kml_entry_name=kml_entry_name,
    )


def _stage_result(batch: FiberTopologySourceBatch, *, created: bool):
    return FiberSourceStageResult(
        batch_id=batch.id,
        created=created,
        status=batch.status,
        feature_count=batch.feature_count,
        blocker_count=batch.blocker_count,
        candidate_count=batch.candidate_count,
        new_count=batch.new_count,
        unchanged_count=batch.unchanged_count,
        file_sha256=batch.file_sha256,
        manifest_sha256=batch.manifest_sha256,
    )


def _manifest_sha256(
    plans: tuple[FiberFeatureMatchPlan, ...],
    *,
    source_metadata: dict | None = None,
) -> str:
    manifest_rows = sorted(
        [
            {
                "external_id": plan.feature.external_id,
                "content_sha256": plan.feature.content_sha256,
            }
            for plan in plans
        ],
        key=lambda row: (
            _normalized_key(row["external_id"]),
            row["content_sha256"],
        ),
    )
    if not source_metadata or not source_metadata.get("source_archive_sha256"):
        return _sha256_json(manifest_rows)
    idempotency = {
        "importer_version": str(source_metadata.get("importer_version", "")),
        "source_archive_sha256": str(source_metadata["source_archive_sha256"]),
    }
    full_manifest_sha256 = source_metadata.get("full_manifest_sha256")
    if full_manifest_sha256:
        idempotency["full_manifest_sha256"] = str(full_manifest_sha256)
    return _sha256_json(
        {
            "features": manifest_rows,
            "idempotency": idempotency,
        }
    )


def persist_fiber_preview(
    db: Session,
    preview: FiberSourcePreview,
    *,
    plans: tuple[FiberFeatureMatchPlan, ...],
    source_name: str,
    created_by: str,
    source_metadata: dict | None = None,
    command_key_sha256: str | None = None,
    command_fingerprint_sha256: str | None = None,
) -> FiberSourceStageResult:
    """Persist normalized evidence in the caller-owned transaction and flush only."""

    manifest_sha256 = _manifest_sha256(plans, source_metadata=source_metadata)
    existing = db.scalar(
        select(FiberTopologySourceBatch).where(
            FiberTopologySourceBatch.source_system == preview.source_system,
            FiberTopologySourceBatch.profile == preview.profile.name,
            FiberTopologySourceBatch.manifest_sha256 == manifest_sha256,
        )
    )
    if existing is not None:
        return _stage_result(existing, created=False)

    status_counts = Counter(plan.match_status for plan in plans)
    blocker_count = status_counts.get("blocked", 0)
    candidate_count = sum(
        status_counts.get(status, 0)
        for status in ("exact_external", "candidate", "ambiguous")
    )
    metadata = {
        "normalization_version": NORMALIZATION_VERSION,
        "kml_entry_name": preview.kml_entry_name,
        "expected_geometry_type": preview.profile.expected_geometry_type,
        **(source_metadata or {}),
    }
    batch = FiberTopologySourceBatch(
        source_system=preview.source_system,
        profile=preview.profile.name,
        source_name=source_name,
        asset_type=preview.profile.asset_type,
        external_id_key=preview.profile.external_id_key,
        file_sha256=preview.file_sha256,
        manifest_sha256=manifest_sha256,
        command_key_sha256=command_key_sha256,
        command_fingerprint_sha256=command_fingerprint_sha256,
        status="blocked" if blocker_count else "staged",
        feature_count=len(plans),
        blocker_count=blocker_count,
        candidate_count=candidate_count,
        unchanged_count=status_counts.get("unchanged", 0),
        new_count=status_counts.get("new", 0),
        source_metadata=metadata,
        created_by=created_by,
    )
    db.add(batch)
    db.flush()
    for plan in plans:
        feature = plan.feature
        db.add(
            FiberTopologyStagedFeature(
                batch_id=batch.id,
                row_number=feature.row_number,
                asset_type=feature.asset_type,
                external_id=feature.external_id,
                display_name=feature.display_name,
                geometry_type=feature.geometry_type,
                geometry_geojson=feature.geometry_geojson,
                source_properties=feature.source_properties,
                content_sha256=feature.content_sha256,
                geometry_sha256=feature.geometry_sha256,
                match_status=plan.match_status,
                blocker_codes=list(feature.blocker_codes),
                match_reasons=list(plan.match_reasons),
                candidate_asset_ids=list(plan.candidate_asset_ids),
                canonical_asset_type=plan.canonical_asset_type,
                canonical_asset_id=plan.canonical_asset_id,
                prior_feature_id=plan.prior_feature_id,
            )
        )
    db.flush()
    return _stage_result(batch, created=True)


def _persist_preview(
    db: Session,
    preview: FiberSourcePreview,
    *,
    plans: tuple[FiberFeatureMatchPlan, ...],
    source_name: str,
    created_by: str,
    source_metadata: dict | None = None,
) -> FiberSourceStageResult:
    """Compatibility boundary for operator scripts pending typed CLI migration."""

    try:
        result = persist_fiber_preview(
            db,
            preview,
            plans=plans,
            source_name=source_name,
            created_by=created_by,
            source_metadata=source_metadata,
        )
        db.commit()
        return result
    except IntegrityError:
        db.rollback()
        manifest_sha256 = _manifest_sha256(plans, source_metadata=source_metadata)
        existing = db.scalar(
            select(FiberTopologySourceBatch).where(
                FiberTopologySourceBatch.source_system == preview.source_system,
                FiberTopologySourceBatch.profile == preview.profile.name,
                FiberTopologySourceBatch.manifest_sha256 == manifest_sha256,
            )
        )
        if existing is None:
            raise
        return _stage_result(existing, created=False)


def stage_fiber_preview_batch(
    db: Session,
    preview: FiberSourcePreview,
    *,
    start: int,
    stop: int,
    source_name: str,
    created_by: str,
    source_metadata: dict | None = None,
) -> FiberSourceStageResult:
    """Persist one bounded slice using the full-source match classification."""

    actor = created_by.strip()
    if not actor:
        raise ValueError("created_by is required for staged topology evidence")
    if start < 0 or stop <= start or stop > preview.feature_count:
        raise ValueError("preview batch bounds are invalid")
    metadata = {
        "batch_start": start + 1,
        "batch_stop": stop,
        **(source_metadata or {}),
    }
    metadata["full_manifest_sha256"] = _manifest_sha256(
        preview.features,
        source_metadata=metadata,
    )
    return _persist_preview(
        db,
        preview,
        plans=preview.features[start:stop],
        source_name=source_name,
        created_by=actor,
        source_metadata=metadata,
    )


def stage_fiber_source(
    db: Session,
    path: str | Path,
    profile_name: str,
    *,
    created_by: str,
) -> FiberSourceStageResult:
    """Persist an immutable source snapshot; never mutate canonical assets."""
    actor = created_by.strip()
    if not actor:
        raise ValueError("created_by is required for staged topology evidence")
    preview = preview_fiber_source(db, path, profile_name)
    return _persist_preview(
        db,
        preview,
        plans=preview.features,
        source_name=preview.source_name,
        created_by=actor,
    )


__all__ = [
    "FiberFeatureMatchPlan",
    "FiberSourcePreview",
    "FiberSourceProfile",
    "FiberSourceStageResult",
    "mixed_geometry_compatible",
    "revalidate_mixed_features",
    "SOURCE_PROFILES",
    "preview_fiber_source",
    "preview_uploaded_fiber_source",
    "persist_fiber_preview",
    "source_profile",
    "stage_fiber_preview_batch",
    "stage_fiber_source",
]
