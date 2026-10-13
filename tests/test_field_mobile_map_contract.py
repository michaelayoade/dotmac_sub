"""Pin mobile map reads to the real API query and serialized page schema."""

from pathlib import Path
from uuid import UUID

from fastapi import FastAPI

from app.api.field.map_assets import router
from app.schemas.common import ListResponse
from app.schemas.field import FieldMapAsset
from app.services.field.map_assets import SUPPORTED_FIELD_MAP_ASSET_TYPES

ROOT = Path(__file__).resolve().parents[1]


def test_mobile_map_query_and_layers_match_server_schema() -> None:
    app = FastAPI()
    app.include_router(router)
    operation = app.openapi()["paths"]["/map-assets"]["get"]
    parameters = {item["name"]: item for item in operation["parameters"]}
    assert parameters["asset_type"]["schema"]["anyOf"][0]["type"] == "array"
    assert {"limit", "offset"} <= parameters.keys()
    assert "types" not in parameters
    client = (
        ROOT / "field_mobile/lib/features/today/map_assets_repository.dart"
    ).read_text()
    assert "'asset_type': [type]" in client
    assert "'offset': offset" in client
    assert "'updated_since':" not in client
    models = (ROOT / "field_mobile/lib/features/today/map_models.dart").read_text()
    labels = models.split("const mapAssetTypeLabels = {", 1)[1].split("};", 1)[0]
    for asset_type in SUPPORTED_FIELD_MAP_ASSET_TYPES:
        assert f"'{asset_type}':" in labels
    assert "'olt':" not in labels
    assert "'fdh':" not in labels


def test_mobile_page_fixture_matches_real_response_model() -> None:
    # FastAPI's declared response model serializes UUIDs and UTC timestamps;
    # the list envelope deliberately carries neither tombstones nor server time.
    fixture = ROOT / "field_mobile/test/fixtures/map_assets_page.json"
    page = ListResponse[FieldMapAsset].model_validate_json(fixture.read_text())
    serialized = page.model_dump(mode="json")
    assert set(serialized) == {"items", "count", "limit", "offset"}
    assert page.count == len(page.items) == 1
    assert page.items[0].id == UUID("00000000-0000-4000-8000-000000000001")
    assert serialized["items"][0]["type"] == "fdh_cabinet"
