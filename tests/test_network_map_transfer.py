from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from typing import cast
from uuid import uuid4
from zipfile import ZipFile

from defusedxml.ElementTree import fromstring
from sqlalchemy.orm import Session

from app.schemas.network_map_transfer import (
    NetworkMapExportLayer,
    NetworkMapExportScope,
    NetworkMapKmzExportQuery,
)
from app.services import network_map_transfer
from app.services.network_map_contracts import (
    NetworkMapFeature,
    NetworkMapFeatureProperties,
    NetworkMapFeatureType,
    NetworkMapPointGeometry,
)


def test_kmz_export_escapes_feature_text_and_remains_parseable(monkeypatch) -> None:
    feature = NetworkMapFeature(
        geometry=NetworkMapPointGeometry(longitude=7.49508, latitude=9.05785),
        properties=NetworkMapFeatureProperties(
            id=uuid4(),
            feature_type=NetworkMapFeatureType.pop_site,
            name='Abuja & <script>alert("unsafe")</script>',
            code='POP-"ONE"',
        ),
    )
    monkeypatch.setattr(
        network_map_transfer.network_map,
        "build_network_map_projection",
        lambda *, db: SimpleNamespace(features=(feature,)),
    )

    outcome = network_map_transfer.export_network_map_kmz(
        db=cast(Session, object()),
        query=NetworkMapKmzExportQuery(
            layers=(NetworkMapExportLayer.infrastructure,),
            scope=NetworkMapExportScope.all,
            bounds=None,
            include_customers=False,
        ),
    )

    with ZipFile(BytesIO(outcome.content)) as archive:
        document = fromstring(archive.read("doc.kml"))

    namespace = {"kml": "http://www.opengis.net/kml/2.2"}
    placemark = document.find(".//kml:Placemark", namespace)
    assert placemark is not None
    assert (
        placemark.findtext("kml:name", namespaces=namespace) == feature.properties.name
    )
    assert placemark.find(".//kml:script", namespace) is None
    assert outcome.feature_count == 1
