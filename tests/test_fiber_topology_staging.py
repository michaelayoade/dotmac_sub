from __future__ import annotations

import socket
import zipfile
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.models.network import FdhCabinet, FiberAccessPoint
from app.schemas.network_map_transfer import (
    NetworkMapImportAssetType,
    NetworkMapImportProposalEligibility,
)
from app.services.network.fiber_topology_staging import (
    SOURCE_PROFILES,
    FiberAssetType,
    FiberFeatureMatchPlan,
    ParsedFiberFeature,
    preview_fiber_source,
    preview_uploaded_fiber_source,
    stage_fiber_preview_batch,
    stage_fiber_source,
)
from app.services.network_map_transfer import (
    _imported_geometry,
    _proposal_eligibility,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_import_review_asset_types_match_the_fiber_domain_vocabulary():
    supported = {
        FiberAssetType.fiber_segment,
        FiberAssetType.fiber_access_point,
        FiberAssetType.fdh_cabinet,
        FiberAssetType.splice_closure,
        FiberAssetType.service_building,
        FiberAssetType.support_structure,
        FiberAssetType.unclassified,
        FiberAssetType.unsupported,
    }
    assert {
        FiberAssetType(value.value) for value in NetworkMapImportAssetType
    } == supported


def test_proposal_eligibility_is_projected_by_the_import_owner():
    def plan(
        *,
        asset_type: FiberAssetType,
        geometry_type: str,
        match_status: str = "new",
        external_id: str | None = "source-1",
        blockers: tuple[str, ...] = (),
    ) -> FiberFeatureMatchPlan:
        feature = ParsedFiberFeature(
            row_number=1,
            asset_type=asset_type,
            external_id=external_id,
            display_name="Imported asset",
            geometry_type=geometry_type,
            geometry_geojson={"type": geometry_type, "coordinates": []},
            source_properties={},
            content_sha256="a" * 64,
            geometry_sha256="b" * 64,
            blocker_codes=blockers,
        )
        return FiberFeatureMatchPlan(
            feature=feature,
            match_status=match_status,
            match_reasons=(),
            candidate_asset_ids=(),
            canonical_asset_type=None,
            canonical_asset_id=None,
            prior_feature_id=None,
        )

    assert (
        _proposal_eligibility(
            plan(asset_type=FiberAssetType.fdh_cabinet, geometry_type="Point")
        )
        is NetworkMapImportProposalEligibility.eligible
    )
    assert (
        _proposal_eligibility(
            plan(
                asset_type=FiberAssetType.fdh_cabinet,
                geometry_type="Point",
                match_status="candidate",
            )
        )
        is NetworkMapImportProposalEligibility.matched
    )
    assert (
        _proposal_eligibility(
            plan(asset_type=FiberAssetType.fiber_segment, geometry_type="LineString")
        )
        is NetworkMapImportProposalEligibility.unsupported_asset_type
    )
    assert (
        _proposal_eligibility(
            plan(asset_type=FiberAssetType.fdh_cabinet, geometry_type="Polygon")
        )
        is NetworkMapImportProposalEligibility.non_point_geometry
    )
    assert (
        _proposal_eligibility(
            plan(
                asset_type=FiberAssetType.support_structure,
                geometry_type="Point",
                external_id=None,
            )
        )
        is NetworkMapImportProposalEligibility.source_id_required
    )
    assert (
        _proposal_eligibility(
            plan(
                asset_type=FiberAssetType.support_structure,
                geometry_type="Point",
                external_id="x" * 81,
            )
        )
        is NetworkMapImportProposalEligibility.source_id_too_long
    )
    assert (
        _proposal_eligibility(
            plan(
                asset_type=FiberAssetType.fdh_cabinet,
                geometry_type="Point",
                blockers=("invalid_coordinate",),
            )
        )
        is NetworkMapImportProposalEligibility.blocked
    )


def _geometry_xml(geometry_type: str, coordinates: str) -> str:
    if geometry_type == "Polygon":
        return (
            "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
            f"{coordinates}"
            "</coordinates></LinearRing></outerBoundaryIs></Polygon>"
        )
    return (
        f"<{geometry_type}><coordinates>{coordinates}</coordinates></{geometry_type}>"
    )


def _placemark(
    *,
    name: str,
    properties: dict[str, str],
    geometry_type: str,
    coordinates: str,
    placemark_id: str | None = None,
    description: str | None = None,
    style_xml: str = "",
) -> str:
    simple_data = "".join(
        f'<SimpleData name="{escape(key)}">{escape(value)}</SimpleData>'
        for key, value in properties.items()
    )
    id_attribute = f' id="{escape(placemark_id)}"' if placemark_id else ""
    return (
        f"<Placemark{id_attribute}>"
        f"<name>{escape(name)}</name>"
        f"<description>{escape(description or '')}</description>"
        f"{style_xml}"
        "<ExtendedData><SchemaData>"
        f"{simple_data}"
        "</SchemaData></ExtendedData>"
        f"{_geometry_xml(geometry_type, coordinates)}"
        "</Placemark>"
    )


def _write_kmz(
    tmp_path: Path,
    filename: str,
    placemarks: list[str],
    *,
    extra_archive_entry: bool = False,
) -> Path:
    path = tmp_path / filename
    kml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"{''.join(placemarks)}"
        "</Document></kml>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("doc.kml", kml)
        if extra_archive_entry:
            archive.writestr("files/source-note.txt", "same normalized source")
    return path


def _polygon(seed: float) -> str:
    return f"7.{seed:.0f},9.0 7.{seed:.0f},9.1 7.{seed + 1:.0f},9.1 7.{seed:.0f},9.0"


def test_preview_is_deterministic_and_flags_source_collisions(db_session, tmp_path):
    path = _write_kmz(
        tmp_path,
        "cabinets.kmz",
        [
            _placemark(
                name="Same cabinet",
                properties={"fibermngrid": "CAB-1", "name": "Same cabinet"},
                geometry_type="Polygon",
                coordinates=_polygon(1),
            ),
            _placemark(
                name="Same cabinet",
                properties={"fibermngrid": "CAB-2", "name": "Same cabinet"},
                geometry_type="Polygon",
                coordinates=_polygon(1),
            ),
            _placemark(
                name="Missing identity",
                properties={"name": "Missing identity"},
                geometry_type="Polygon",
                coordinates=_polygon(3),
            ),
        ],
    )

    first = preview_fiber_source(db_session, path, "osp_cabinets")
    second = preview_fiber_source(db_session, path, "osp_cabinets")

    assert first.file_sha256 == second.file_sha256
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.status_counts == {"candidate": 2, "blocked": 1}
    assert first.blocker_count == 1
    assert "duplicate_source_name" in first.features[0].match_reasons
    assert "duplicate_source_geometry" in first.features[0].match_reasons
    assert "missing_external_id" in first.features[2].feature.blocker_codes


def test_preview_suggests_external_code_before_normalized_name(db_session, tmp_path):
    canonical = FdhCabinet(
        name="Existing Cabinet",
        code="CAB-1",
        latitude=9.0,
        longitude=7.1,
    )
    db_session.add(canonical)
    db_session.commit()

    path = _write_kmz(
        tmp_path,
        "cabinets.kmz",
        [
            _placemark(
                name="Renamed source cabinet",
                properties={"fibermngrid": "CAB-1", "name": "Renamed cabinet"},
                geometry_type="Polygon",
                coordinates=_polygon(1),
            ),
            _placemark(
                name="Existing Cabinet",
                properties={"fibermngrid": "CAB-2", "name": "Existing Cabinet"},
                geometry_type="Polygon",
                coordinates=_polygon(3),
            ),
        ],
    )

    preview = preview_fiber_source(db_session, path, "osp_cabinets")

    assert preview.features[0].match_status == "exact_external"
    assert preview.features[0].canonical_asset_id == canonical.id
    assert preview.features[1].match_status == "candidate"
    assert preview.features[1].canonical_asset_id == canonical.id
    assert preview.features[1].match_reasons == ("canonical_normalized_name_match",)


def test_stage_is_idempotent_and_never_creates_canonical_assets(db_session, tmp_path):
    path = _write_kmz(
        tmp_path,
        "access-points.kmz",
        [
            _placemark(
                name="FAT-1",
                properties={"access_pointid": "FAT-1", "Name": "FAT-1"},
                geometry_type="Polygon",
                coordinates=_polygon(1),
            )
        ],
    )

    first = stage_fiber_source(
        db_session, path, "osp_access_points", created_by="pytest"
    )
    second = stage_fiber_source(
        db_session, path, "osp_access_points", created_by="pytest-replay"
    )

    assert first.created is True
    assert second.created is False
    assert second.batch_id == first.batch_id
    assert db_session.query(FiberTopologySourceBatch).count() == 1
    assert db_session.query(FiberTopologyStagedFeature).count() == 1
    assert db_session.query(FiberAccessPoint).count() == 0


def test_mixed_network_map_stages_supported_assets_and_geometry_types(
    db_session, tmp_path
):
    path = _write_kmz(
        tmp_path,
        "network-map.kmz",
        [
            _placemark(
                name="Route 1",
                properties={
                    "dotmac_asset_type": "fiber_segment",
                    "dotmac_asset_id": "SEG-1",
                },
                geometry_type="LineString",
                coordinates="7.1,9.0 7.2,9.1",
            ),
            _placemark(
                name="Cabinet 1",
                properties={
                    "dotmac_asset_type": "fdh_cabinet",
                    "dotmac_asset_id": "CAB-1",
                },
                geometry_type="Point",
                coordinates="7.3,9.2",
            ),
            _placemark(
                name="Access point 1",
                properties={
                    "dotmac_asset_type": "access_point",
                    "dotmac_asset_id": "AP-1",
                },
                geometry_type="Polygon",
                coordinates=_polygon(4),
            ),
        ],
    )

    preview = preview_fiber_source(db_session, path, "mixed_network_map")
    staged = stage_fiber_source(
        db_session, path, "mixed_network_map", created_by="pytest"
    )
    features = (
        db_session.query(FiberTopologyStagedFeature)
        .order_by(FiberTopologyStagedFeature.row_number)
        .all()
    )

    assert preview.profile.asset_type == "mixed_network_map"
    assert preview.blocker_count == 0
    assert staged.status == "staged"
    assert [(feature.asset_type, feature.geometry_type) for feature in features] == [
        ("fiber_segment", "LineString"),
        ("fdh_cabinet", "Point"),
        ("fiber_access_point", "Polygon"),
    ]
    assert [feature.external_id for feature in features] == [
        "SEG-1",
        "CAB-1",
        "AP-1",
    ]
    assert db_session.query(FdhCabinet).count() == 0
    assert db_session.query(FiberAccessPoint).count() == 0


def test_mixed_kml_stages_features_without_profile_specific_ids(db_session, tmp_path):
    path = tmp_path / "network-map.kml"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        + _placemark(
            name="Unnumbered path",
            properties={"dotmac_asset_type": "fiber_segment"},
            geometry_type="LineString",
            coordinates="7.1,9.0 7.2,9.1",
        )
        + _placemark(
            name="Unnumbered cabinet",
            properties={"dotmac_asset_type": "fdh_cabinet"},
            geometry_type="Point",
            coordinates="7.3,9.2",
        )
        + "</Document></kml>",
        encoding="utf-8",
    )

    preview = preview_fiber_source(db_session, path, "mixed_network_map")
    staged = stage_fiber_source(
        db_session, path, "mixed_network_map", created_by="pytest"
    )
    features = (
        db_session.query(FiberTopologyStagedFeature)
        .order_by(FiberTopologyStagedFeature.row_number)
        .all()
    )

    assert preview.blocker_count == 0
    assert staged.status == "staged"
    assert [feature.external_id for feature in features] == [None, None]
    assert [feature.geometry_type for feature in features] == ["LineString", "Point"]


def test_mixed_network_map_preserves_unsupported_feature_geometry_for_review(
    db_session, tmp_path
):
    path = _write_kmz(
        tmp_path,
        "mixed-map.kmz",
        [
            _placemark(
                name="Private customer name",
                properties={
                    "dotmac_asset_type": "customer",
                    "dotmac_asset_id": "CUSTOMER-1",
                    "address": "Private customer address",
                    "name": "Private customer name",
                },
                geometry_type="Point",
                coordinates="7.3,9.2",
            )
        ],
    )

    staged = stage_fiber_source(
        db_session, path, "mixed_network_map", created_by="pytest"
    )
    feature = db_session.query(FiberTopologyStagedFeature).one()

    assert staged.status == "blocked"
    assert staged.blocker_count == 1
    assert feature.blocker_codes == ["unsupported_asset_type"]
    assert feature.asset_type == "unsupported"
    assert feature.display_name == "Private customer name"
    assert feature.external_id is None
    assert feature.source_properties == {}
    assert feature.geometry_type == "Point"
    assert feature.geometry_geojson == {"type": "Point", "coordinates": [7.3, 9.2]}


def test_mixed_kml_preserves_standard_metadata_geometry_and_safe_icon_refs(
    db_session, monkeypatch
):
    def fail_network(*_args, **_kwargs):
        raise AssertionError("KML import must never fetch remote resources")

    monkeypatch.setattr(socket, "getaddrinfo", fail_network)
    monkeypatch.setattr(socket, "create_connection", fail_network)
    kml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <kml xmlns="http://www.opengis.net/kml/2.2"><Document>
      <Style id="google-pin"><IconStyle><scale>1.2</scale><Icon>
        <href>https://maps.google.com/mapfiles/kml/paddle/red-circle.png</href>
      </Icon></IconStyle></Style>
      <Placemark id="KML-FDH-1"><name>Cabinet by park</name>
        <description><![CDATA[<b>Checked</b> by survey]]></description>
        <styleUrl>#google-pin</styleUrl><ExtendedData>
          <Data name="asset_type"><value>fdh_cabinet</value></Data>
          <Data name="dotmac_asset_id"><value>FDH-KML-1</value></Data>
          <Data name="code"><value>FDH-KML-1</value></Data>
        </ExtendedData><Point><coordinates>7.1,9.0</coordinates></Point>
      </Placemark>
      <Placemark><name>Fiber line</name><ExtendedData>
        <Data name="type"><value>fiber_segment</value></Data>
        <Data name="spanid"><value>SPAN-KML-1</value></Data>
      </ExtendedData><LineString><coordinates>7.1,9.0 7.2,9.1</coordinates></LineString>
      </Placemark>
      <Placemark><name>Access area</name><ExtendedData>
        <Data name="feature_type"><value>access_point</value></Data>
        <Data name="access_pointid"><value>AP-KML-1</value></Data>
      </ExtendedData><Polygon><outerBoundaryIs><LinearRing><coordinates>
        7.0,9.0 7.4,9.0 7.4,9.4 7.0,9.0
      </coordinates></LinearRing></outerBoundaryIs><innerBoundaryIs><LinearRing><coordinates>
        7.1,9.1 7.2,9.1 7.2,9.2 7.1,9.1
      </coordinates></LinearRing></innerBoundaryIs></Polygon>
      </Placemark>
      <Placemark><name>Unclassified survey point</name>
        <Style><IconStyle><Icon><href>https://127.0.0.1/icon.png</href>
        </Icon></IconStyle></Style><Point><coordinates>7.3,9.3</coordinates></Point>
      </Placemark>
      <Placemark><name>Remote icon unavailable or redirected</name>
        <Style><IconStyle><Icon><href>https://icons.example.invalid/redirect</href>
        </Icon></IconStyle></Style><Point><coordinates>7.5,9.5</coordinates></Point>
      </Placemark>
      <Placemark><name>Unspecified fiber route</name>
        <LineString><coordinates>7.4,9.4 7.6,9.6</coordinates></LineString>
      </Placemark>
    </Document></kml>"""

    preview = preview_uploaded_fiber_source(
        db_session,
        content=kml,
        source_name="standard-map.kml",
        profile_name="mixed_network_map",
    )
    features = [plan.feature for plan in preview.features]

    assert [feature.display_name for feature in features] == [
        "Cabinet by park",
        "Fiber line",
        "Access area",
        "Unclassified survey point",
        "Remote icon unavailable or redirected",
        "Unspecified fiber route",
    ]
    assert features[0].external_id == "FDH-KML-1"
    assert features[0].source_properties["kml_placemark_id"] == "KML-FDH-1"
    assert features[0].source_properties["description"] == "Checked by survey"
    assert (
        features[0]
        .source_properties["icon_href"]
        .startswith("https://maps.google.com/")
    )
    assert features[0].source_properties["icon_scale"] == "1.2"
    assert features[1].asset_type.value == "fiber_segment"
    assert features[2].geometry_geojson["type"] == "Polygon"
    assert len(features[2].geometry_geojson["coordinates"]) == 2
    assert features[3].asset_type.value == "unclassified"
    assert features[3].geometry_geojson["type"] == "Point"
    assert features[3].blocker_codes == ("missing_asset_type",)
    assert "icon_href" not in features[3].source_properties
    assert (
        "icon_reference_internal_address"
        in features[3].source_properties["resource_warnings"]
    )
    # This unverified host is preserved as metadata and is never fetched. The
    # preview keeps the point geometry and falls back to its default symbol.
    assert features[4].source_properties["icon_href"].endswith("/redirect")
    assert features[4].geometry_geojson["type"] == "Point"
    assert features[5].asset_type.value == "unclassified"
    assert features[5].suggested_asset_type.value == "fiber_segment"
    assert "missing_asset_type" in features[5].blocker_codes


def test_network_links_are_reported_and_not_expanded(db_session):
    kml = b"""<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
      <NetworkLink><name>Remote map layer</name><Link>
        <href>https://maps.example.invalid/remote.kml</href>
      </Link></NetworkLink>
      <Placemark><name>Local cabinet</name><ExtendedData>
        <Data name="asset_type"><value>fdh_cabinet</value></Data>
      </ExtendedData><Point><coordinates>7.2,9.1</coordinates></Point></Placemark>
      </Document></kml>"""
    preview = preview_uploaded_fiber_source(
        db_session,
        content=kml,
        source_name="network-link-with-local.kml",
        profile_name="mixed_network_map",
    )
    assert len(preview.features) == 1
    assert preview.features[0].feature.display_name == "Local cabinet"
    assert preview.features[0].feature.geometry_geojson["type"] == "Point"
    assert any(
        "network_link_not_expanded:Remote map layer" in warning
        for warning in preview.features[0]
        .feature.source_properties["resource_warnings"]
        .split("|")
    )

    link_only = b"""<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
      <NetworkLink><name>Remote map layer</name><Link>
        <href>https://maps.example.invalid/remote.kml</href>
      </Link></NetworkLink></Document></kml>"""
    with pytest.raises(ValueError, match="Remote map layer.*upload it directly"):
        preview_uploaded_fiber_source(
            db_session,
            content=link_only,
            source_name="network-link.kml",
            profile_name="mixed_network_map",
        )


def test_kml_error_and_kmz_archive_limits_explain_how_to_fix(db_session):
    with pytest.raises(ValueError, match="line 1, column"):
        preview_uploaded_fiber_source(
            db_session,
            content=b"<kml><Document><Placemark>",
            source_name="broken.kml",
            profile_name="mixed_network_map",
        )

    archive = BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("doc.kml", b" " * 300_000)
    with pytest.raises(ValueError, match="compression ratio.*Re-export"):
        preview_uploaded_fiber_source(
            db_session,
            content=archive.getvalue(),
            source_name="compressed.kmz",
            profile_name="mixed_network_map",
        )

    too_many_entries = BytesIO()
    with zipfile.ZipFile(
        too_many_entries, "w", compression=zipfile.ZIP_STORED
    ) as zipped:
        zipped.writestr("doc.kml", b"<kml/>")
        for index in range(64):
            zipped.writestr(f"icons/{index}.png", b"x")
    with pytest.raises(ValueError, match="more than 64 files"):
        preview_uploaded_fiber_source(
            db_session,
            content=too_many_entries.getvalue(),
            source_name="too-many-entries.kmz",
            profile_name="mixed_network_map",
        )

    with pytest.raises(ValueError, match="25 MB upload limit"):
        preview_uploaded_fiber_source(
            db_session,
            content=b"x" * (25 * 1024 * 1024 + 1),
            source_name="too-large.kmz",
            profile_name="mixed_network_map",
        )


def test_preview_geometry_serialization_preserves_polygon_rings_and_components():
    polygon = {
        "type": "Polygon",
        "coordinates": [
            [[7.0, 9.0], [7.1, 9.0], [7.1, 9.1], [7.0, 9.0]],
            [[7.02, 9.02], [7.03, 9.02], [7.03, 9.03], [7.02, 9.02]],
        ],
    }
    collection = {
        "type": "GeometryCollection",
        "geometries": [
            {"type": "Point", "coordinates": [7.0, 9.0]},
            {"type": "LineString", "coordinates": [[7.0, 9.0], [7.1, 9.1]]},
        ],
    }
    assert _imported_geometry(polygon).to_transport() == polygon
    assert _imported_geometry(collection).to_transport() == collection


def test_mixed_network_map_blocks_geometry_that_does_not_match_asset_type(
    db_session, tmp_path
):
    path = _write_kmz(
        tmp_path,
        "mixed-map.kmz",
        [
            _placemark(
                name="Fiber path with point geometry",
                properties={
                    "dotmac_asset_type": "fiber_segment",
                    "dotmac_asset_id": "SEG-1",
                },
                geometry_type="Point",
                coordinates="7.3,9.2",
            )
        ],
    )

    staged = stage_fiber_source(
        db_session, path, "mixed_network_map", created_by="pytest"
    )
    feature = db_session.query(FiberTopologyStagedFeature).one()

    assert staged.status == "blocked"
    assert feature.blocker_codes == ["unexpected_geometry_type"]
    assert feature.asset_type == "fiber_segment"


def test_mixed_network_map_suggests_exact_match_by_exported_asset_id(
    db_session, tmp_path
):
    canonical = FdhCabinet(
        name="Canonical cabinet",
        code="CANON-CAB-1",
        latitude=9.0,
        longitude=7.1,
    )
    db_session.add(canonical)
    db_session.commit()
    path = _write_kmz(
        tmp_path,
        "network-map.kmz",
        [
            _placemark(
                name="Different display name",
                properties={
                    "dotmac_asset_type": "fdh_cabinet",
                    "dotmac_asset_id": str(canonical.id),
                },
                geometry_type="Point",
                coordinates="7.3,9.2",
            )
        ],
    )

    preview = preview_fiber_source(db_session, path, "mixed_network_map")

    assert preview.features[0].match_status == "exact_external"
    assert preview.features[0].canonical_asset_id == canonical.id


def test_repackaged_identical_manifest_is_idempotent(db_session, tmp_path):
    feature = _placemark(
        name="FAT-1",
        properties={"access_pointid": "FAT-1", "Name": "FAT-1"},
        geometry_type="Polygon",
        coordinates=_polygon(1),
    )
    first_path = _write_kmz(tmp_path, "first.kmz", [feature])
    repackaged_path = _write_kmz(
        tmp_path,
        "repackaged.kmz",
        [feature],
        extra_archive_entry=True,
    )

    first_preview = preview_fiber_source(db_session, first_path, "osp_access_points")
    repackaged_preview = preview_fiber_source(
        db_session, repackaged_path, "osp_access_points"
    )
    first = stage_fiber_source(
        db_session, first_path, "osp_access_points", created_by="pytest"
    )
    repackaged = stage_fiber_source(
        db_session,
        repackaged_path,
        "osp_access_points",
        created_by="pytest",
    )

    assert first_preview.file_sha256 != repackaged_preview.file_sha256
    assert first_preview.manifest_sha256 == repackaged_preview.manifest_sha256
    assert first.created is True
    assert repackaged.created is False
    assert repackaged.batch_id == first.batch_id


def test_changed_stable_identity_requires_review_and_preserves_lineage(
    db_session, tmp_path
):
    first_path = _write_kmz(
        tmp_path,
        "paths-v1.kmz",
        [
            _placemark(
                name="SPAN-1",
                properties={"spanid": "SPAN-1"},
                geometry_type="LineString",
                coordinates="7.1,9.0 7.2,9.1",
            )
        ],
    )
    first = stage_fiber_source(db_session, first_path, "osp_paths", created_by="pytest")
    first_feature = db_session.query(FiberTopologyStagedFeature).one()

    second_path = _write_kmz(
        tmp_path,
        "paths-v2.kmz",
        [
            _placemark(
                name="SPAN-1",
                properties={"spanid": "SPAN-1"},
                geometry_type="LineString",
                coordinates="7.1,9.0 7.3,9.2",
            )
        ],
    )
    preview = preview_fiber_source(db_session, second_path, "osp_paths")

    assert first.created is True
    assert preview.features[0].match_status == "candidate"
    assert "changed_source_identity" in preview.features[0].match_reasons
    assert preview.features[0].prior_feature_id == first_feature.id

    second = stage_fiber_source(
        db_session, second_path, "osp_paths", created_by="pytest"
    )
    assert second.created is True
    assert db_session.query(FiberTopologySourceBatch).count() == 2
    staged = (
        db_session.query(FiberTopologyStagedFeature)
        .order_by(FiberTopologyStagedFeature.created_at.desc())
        .first()
    )
    assert staged.prior_feature_id == first_feature.id


def test_bounded_stage_preserves_full_source_duplicate_classification(
    db_session, tmp_path
):
    path = _write_kmz(
        tmp_path,
        "crm-cabinets.kmz",
        [
            _placemark(
                name="Shared name",
                properties={"crm_id": "CRM-1", "name": "Shared name"},
                geometry_type="Point",
                coordinates="7.1,9.0",
            ),
            _placemark(
                name="Shared name",
                properties={"crm_id": "CRM-2", "name": "Shared name"},
                geometry_type="Point",
                coordinates="7.2,9.1",
            ),
        ],
    )
    preview = preview_fiber_source(db_session, path, "crm_fdh_cabinets")
    assert preview.status_counts == {"candidate": 2}

    first = stage_fiber_preview_batch(
        db_session,
        preview,
        start=0,
        stop=1,
        source_name="crm_fdh_cabinets-00001.kml",
        created_by="pytest",
        source_metadata={"source_archive_sha256": "a" * 64},
    )
    second = stage_fiber_preview_batch(
        db_session,
        preview,
        start=1,
        stop=2,
        source_name="crm_fdh_cabinets-00002.kml",
        created_by="pytest",
        source_metadata={"source_archive_sha256": "a" * 64},
    )

    assert first.created is True
    assert second.created is True
    assert first.candidate_count == second.candidate_count == 1
    assert {
        feature.match_status
        for feature in db_session.query(FiberTopologyStagedFeature).all()
    } == {"candidate"}
    full_manifest_hashes = {
        batch.source_metadata["full_manifest_sha256"]
        for batch in db_session.query(FiberTopologySourceBatch).all()
    }
    assert len(full_manifest_hashes) == 1
    assert full_manifest_hashes != {preview.manifest_sha256}


def test_crm_bounded_stage_idempotency_is_archive_aware(db_session, tmp_path):
    path = _write_kmz(
        tmp_path,
        "crm-cabinet.kmz",
        [
            _placemark(
                name="FDH 1",
                properties={"crm_id": "CRM-1", "name": "FDH 1"},
                geometry_type="Point",
                coordinates="7.1,9.0",
            ),
        ],
    )
    preview = preview_fiber_source(db_session, path, "crm_fdh_cabinets")
    first_metadata = {
        "importer_version": "stage_crm_network_map:v2",
        "source_archive_sha256": "a" * 64,
    }
    second_metadata = {
        "importer_version": "stage_crm_network_map:v2",
        "source_archive_sha256": "b" * 64,
    }

    first = stage_fiber_preview_batch(
        db_session,
        preview,
        start=0,
        stop=1,
        source_name="crm_fdh_cabinets-00001-a.kml",
        created_by="pytest",
        source_metadata=first_metadata,
    )
    same_archive = stage_fiber_preview_batch(
        db_session,
        preview,
        start=0,
        stop=1,
        source_name="crm_fdh_cabinets-00001-a-rerun.kml",
        created_by="pytest",
        source_metadata=first_metadata,
    )
    new_archive = stage_fiber_preview_batch(
        db_session,
        preview,
        start=0,
        stop=1,
        source_name="crm_fdh_cabinets-00001-b.kml",
        created_by="pytest",
        source_metadata=second_metadata,
    )

    assert first.created is True
    assert same_archive.created is False
    assert same_archive.batch_id == first.batch_id
    assert new_archive.created is True
    assert new_archive.batch_id != first.batch_id

    batches = db_session.query(FiberTopologySourceBatch).all()
    assert len(batches) == 2
    assert {batch.source_metadata["source_archive_sha256"] for batch in batches} == {
        "a" * 64,
        "b" * 64,
    }
    assert db_session.query(FiberTopologyStagedFeature).count() == 2


@pytest.mark.parametrize(
    ("profile_name", "expected_count"),
    [
        ("osp_paths", 1600),
        ("osp_access_points", 286),
        ("osp_cabinets", 113),
        ("osp_splice_info", 1021),
        ("osp_buildings", 1146),
        ("osp_air_fiber", 515),
    ],
)
def test_checked_in_osp_sources_have_stable_ids_and_valid_geometry(
    db_session, profile_name, expected_count
):
    profile = SOURCE_PROFILES[profile_name]
    path = PROJECT_ROOT / "docs" / profile.default_filename

    preview = preview_fiber_source(db_session, path, profile_name)

    assert preview.feature_count == expected_count
    assert preview.blocker_count == 0
    assert all(plan.feature.external_id for plan in preview.features)
