"""HTTP regression coverage for field identity and ancillary access mapping."""

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.field import router
from app.db import get_db
from app.models.dispatch import TechnicianProfile
from app.models.field_vendor import FieldVendor, FieldVendorUser
from app.models.network import FdhCabinet
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.models.vendor_routes import Vendor
from app.services.auth_dependencies import require_user_auth


def _vendor_client(db_session, *, native_link: bool, stale_profile: bool = False):
    user = SystemUser(
        first_name="Vendor",
        last_name="Crew",
        email=f"vendor-principal-{uuid4().hex}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    native = Vendor(name="Native vendor", code=f"NV-{uuid4().hex[:8]}")
    db_session.add(native)
    db_session.flush()
    vendor = FieldVendor(
        name="Field crew",
        code=f"FV-{uuid4().hex[:8]}",
        crm_vendor_id=str(native.id) if native_link else None,
    )
    db_session.add(vendor)
    db_session.flush()
    db_session.add(
        FieldVendorUser(vendor_id=vendor.id, system_user_id=user.id, role="crew")
    )
    if stale_profile:
        db_session.add(
            TechnicianProfile(
                system_user_id=user.id, person_id=user.id, crm_person_id=None
            )
        )
    db_session.commit()
    auth = {"principal_type": "system_user", "principal_id": str(user.id)}
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: auth
    return TestClient(app)


@pytest.mark.parametrize("native_link", [True, False])
def test_vendor_map_relocation_preserves_vendor_principal_fallback(
    db_session, native_link
):
    client = _vendor_client(db_session, native_link=native_link)
    cabinet = FdhCabinet(
        name="Vendor field cabinet",
        code=f"VC-{uuid4().hex[:8]}",
        latitude=9.071,
        longitude=7.451,
    )
    db_session.add(cabinet)
    db_session.commit()
    response = client.patch(
        f"/api/v1/field/map-assets/fdh_cabinet/{cabinet.id}/location",
        json={"latitude": 9.081, "longitude": 7.462},
    )
    assert response.status_code == 200
    assert response.json()["latitude"] == 9.081


@pytest.mark.parametrize("stale_profile", [False, True])
@pytest.mark.parametrize(
    "path",
    [
        "/jobs/unassigned/materials",
        "/jobs/unassigned/equipment",
        "/equipment-custody/mine",
        "/fiber/tests?crm_work_order_id=unassigned",
        "/locations/route?start_lat=9.071&start_lng=7.451",
        "/map-assets/search?q=cabinet",
        "/jobs/unassigned/chat",
    ],
)
def test_vendor_unsupported_staff_routes_map_access_denial(
    db_session, stale_profile, path
):
    client = _vendor_client(db_session, native_link=True, stale_profile=stale_profile)
    response = client.get(f"/api/v1/field{path}")
    assert response.status_code == 403, response.text
    assert (
        response.json()["detail"]["code"] == "operations.field_work_order_access.denied"
    )
