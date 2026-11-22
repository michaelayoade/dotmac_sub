"""Latest-version filtering and bounded source browsing for fiber evidence pages."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.services.network.fiber_staged_inventory import latest_staged_feature_page


def _batch(db_session, *, at: datetime, count: int) -> FiberTopologySourceBatch:
    batch = FiberTopologySourceBatch(
        source_system="pytest_fiber_inventory",
        profile=f"inventory_{uuid.uuid4().hex}",
        source_name="inventory.kmz",
        asset_type="fiber_access_point",
        external_id_key="assetid",
        file_sha256=uuid.uuid4().hex * 2,
        manifest_sha256=uuid.uuid4().hex * 2,
        status="staged",
        feature_count=count,
        blocker_count=0,
        candidate_count=0,
        unchanged_count=0,
        new_count=count,
        source_metadata={},
        created_by="pytest",
        created_at=at,
    )
    db_session.add(batch)
    db_session.flush()
    return batch


def _feature(
    batch, *, row: int, external_id: str, name: str
) -> FiberTopologyStagedFeature:
    return FiberTopologyStagedFeature(
        batch=batch,
        row_number=row,
        asset_type="fiber_access_point",
        external_id=external_id,
        display_name=name,
        geometry_type="Point",
        geometry_geojson={"type": "Point", "coordinates": [7.4, 9.0]},
        source_properties={},
        content_sha256=uuid.uuid4().hex * 2,
        geometry_sha256=uuid.uuid4().hex * 2,
        match_status="new",
        blocker_codes=[],
        match_reasons=[],
        candidate_asset_ids=[],
    )


def test_latest_feature_page_reaches_later_rows_and_filters_after_version_selection(
    db_session,
):
    older = _batch(db_session, at=datetime.now(UTC) - timedelta(days=1), count=26)
    db_session.add_all(
        _feature(
            older,
            row=index + 1,
            external_id=f"FAT-{index:02d}",
            name=f"Source {index:02d}",
        )
        for index in range(26)
    )
    latest = _batch(db_session, at=datetime.now(UTC), count=1)
    db_session.add(_feature(latest, row=1, external_id="FAT-00", name="Current FAT"))
    db_session.flush()

    first = latest_staged_feature_page(
        db_session, asset_types=frozenset({"fiber_access_point"}), page=1
    )
    later = latest_staged_feature_page(
        db_session, asset_types=frozenset({"fiber_access_point"}), page=2
    )
    current = latest_staged_feature_page(
        db_session, asset_types=frozenset({"fiber_access_point"}), query="Current FAT"
    )
    obsolete = latest_staged_feature_page(
        db_session, asset_types=frozenset({"fiber_access_point"}), query="Source 00"
    )

    assert first.total == later.total == 26
    assert len(first.features) == 25
    assert [feature.external_id for feature in later.features] == ["FAT-25"]
    assert current.total == 1
    assert current.features[0].display_name == "Current FAT"
    assert obsolete.total == 0
