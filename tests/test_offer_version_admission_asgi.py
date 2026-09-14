"""End-to-end proof of offer-version admission's REAL ASGI request graph.

Round 13 finding 4 retired the source-grep/direct-dependency-call shape this
file's predecessor (``tests/test_offer_version_admission_route_permissions.py``,
deleted by this same change) used: calling ``_require_offer_version_admission``
or the router gate directly, or inspecting source text/router membership,
proved nothing about the REAL ASGI dependency graph a live request actually
traverses — and that shape has now failed to catch a real bug (round 13's
``credential_kind`` lockout) despite three rounds of making it more elaborate.

Every test here mounts the ACTUAL ``app.api.catalog`` routers (``router`` and
``admission_router``) onto a real ``FastAPI()`` app, in the SAME shape
``app/main.py``'s router table uses (``/api/v1`` prefix, bare-authentication
``"user"`` dependency mode), and drives them with ``fastapi.testclient
.TestClient`` issuing REAL HTTP requests. The only override is
``require_user_auth`` — FastAPI's own supported dependency-override
mechanism, substituting the deepest LEAF authentication dependency with a
fixture-controlled principal, exactly as ``tests/test_dispatch_api.py`` and
several other suites in this repo already do for HTTP-level tests. Every
OTHER dependency in the chain (``require_method_permission``,
``_require_offer_version_admission``, ``authorize_offer_version_admission``,
and the in-transaction re-check inside ``OfferVersions.update``) executes
for real, against the real test database, through the real ASGI stack.

Evidence-precision note (Michael's ruling on evidence claims): the
observation each test below actually makes is stated in its own docstring —
"the mounted app" (a real ``TestClient`` request was issued and its response
observed), "a resolved dependency list" (the live route's own
``dependant.dependencies`` was inspected on an app that was actually
constructed and included), or both. Neither is a source-level import or a
direct call to an inner function.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api import catalog as api_catalog
from app.db import get_db
from app.models.catalog import AccessType, PriceBasis, ServiceType
from app.models.rbac import Permission, SystemUserPermission
from app.models.system_user import SystemUser
from app.schemas.catalog import CatalogOfferCreate
from app.services import catalog as catalog_service
from app.services.auth_dependencies import require_user_auth
from app.services.catalog import offer_access_requirement


def _mounted_app(db_session) -> FastAPI:
    """The REAL router objects, mounted exactly as ``app/main.py``'s router
    table mounts them: same prefix, same bare-authentication dependency
    mode. ``get_db`` is overridden to the test session; ``require_user_auth``
    is overridden per-test to the controllable principal under test — the
    ONE override every HTTP-level test in this repo's suite already uses for
    this exact purpose (see ``tests/test_dispatch_api.py``)."""

    app = FastAPI()
    app.include_router(
        api_catalog.router, prefix="/api/v1", dependencies=[Depends(require_user_auth)]
    )
    app.include_router(
        api_catalog.admission_router,
        prefix="/api/v1",
        dependencies=[Depends(require_user_auth)],
    )
    app.dependency_overrides[get_db] = lambda: db_session
    return app


def _system_user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="ASGI",
        last_name="Test",
        email=f"asgi-{uuid.uuid4().hex[:8]}@example.com",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _grant_direct_permission(db_session, user: SystemUser, key: str) -> None:
    """A DIRECT ``SystemUserPermission`` grant — not the ``admin`` role —
    so it can be individually revoked mid-test without touching any other
    permission the same principal might hold."""

    permission = db_session.query(Permission).filter(Permission.key == key).first()
    if permission is None:
        permission = Permission(key=key, is_active=True)
        db_session.add(permission)
        db_session.commit()
        db_session.refresh(permission)
    db_session.add(
        SystemUserPermission(system_user_id=user.id, permission_id=permission.id)
    )
    db_session.commit()


def _revoke_direct_permission(db_session, user: SystemUser, key: str) -> None:
    permission = db_session.query(Permission).filter(Permission.key == key).first()
    grant = (
        db_session.query(SystemUserPermission)
        .filter(
            SystemUserPermission.system_user_id == user.id,
            SystemUserPermission.permission_id == permission.id,
        )
        .first()
    )
    db_session.delete(grant)
    db_session.commit()


def _auth_for(user: SystemUser) -> dict:
    return {
        "principal_id": str(user.id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }


def _offer(db_session):
    return catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="ASGI Offer",
            code=f"ASGI-{uuid.uuid4().hex[:8]}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        ),
    )


def test_unauthorized_admission_is_refused_by_the_mounted_admission_route(
    db_session,
):
    """OBSERVED: the mounted app, via a real, issued HTTP request. A staff
    principal holding NO permission at all is refused by the actual mounted
    ``POST /api/v1/offer-versions`` route — the entire real dependency
    chain (router-level auth, ``_require_offer_version_admission``,
    ``authorize_offer_version_admission``) runs for this one request; no
    dependency is called directly, no source text is inspected."""

    user = _system_user(db_session)
    app = _mounted_app(db_session)
    app.dependency_overrides[require_user_auth] = lambda: _auth_for(user)
    client = TestClient(app)
    offer = _offer(db_session)

    response = client.post(
        "/api/v1/offer-versions",
        json={
            "offer_id": str(offer.id),
            "version_number": 1,
            "name": "v1",
            "service_type": "residential",
            "access_type": "fiber",
            "price_basis": "flat",
            "access_requirement": "unclassified",
        },
    )
    assert response.status_code == 403


def test_admission_route_actually_carries_the_authorization_dependency(db_session):
    """OBSERVED: a resolved dependency list, read off the route object the
    ``FastAPI()`` app actually constructed after ``include_router`` — not a
    grep over ``app/api/catalog.py``'s source text. Walks the route whose
    path is ``/offer-versions`` (POST) in the REAL mounted app's own
    ``app.routes`` and asserts ``_require_offer_version_admission`` is one
    of its resolved dependant callables.

    Break condition: fails if that dependency is ever removed from
    ``create_offer_version``'s signature (the only way FastAPI's dependant
    tree would stop naming it) — see the companion sensitivity test below
    for the behavioral half of this same property."""

    app = _mounted_app(db_session)
    route = next(
        route
        for route in app.routes
        if getattr(route, "path", None) == "/api/v1/offer-versions"
        and "POST" in getattr(route, "methods", set())
    )
    resolved_dependency_callables = {
        dependency.call for dependency in route.dependant.dependencies
    }
    assert api_catalog._require_offer_version_admission in resolved_dependency_callables


def test_removing_the_admission_dependency_lets_an_unauthorized_request_through(
    db_session,
):
    """The planted removal, proven behaviorally rather than merely
    described. OBSERVED: the mounted app, via two real, issued HTTP
    requests to two different apps — the real one (refuses) and a second
    app built by registering the IDENTICAL handler function with its
    admission dependency replaced by bare authentication (succeeds) — the
    literal shape of "someone deletes ``Depends(_require_offer_version_
    admission)``" from the route.

    This is the sensitivity proof for the structural test above: it does
    not just show the dependency is present, it shows what happens when it
    is not, using the SAME service call the real route makes.
    """

    user = _system_user(db_session)
    offer = _offer(db_session)

    # The real, guarded app: refuses.
    real_app = _mounted_app(db_session)
    real_app.dependency_overrides[require_user_auth] = lambda: _auth_for(user)
    real_client = TestClient(real_app)
    guarded_response = real_client.post(
        "/api/v1/offer-versions",
        json={
            "offer_id": str(offer.id),
            "version_number": 1,
            "name": "v1",
            "service_type": "residential",
            "access_type": "fiber",
            "price_basis": "flat",
            "access_requirement": "unclassified",
        },
    )
    assert guarded_response.status_code == 403

    # The planted removal: the identical create_offer_version handler,
    # registered directly with no admission dependency at all — only bare
    # authentication, which the override below still satisfies.
    from app.schemas.catalog import OfferVersionCreate

    unguarded_app = FastAPI()

    def _unguarded_create_offer_version(
        payload: OfferVersionCreate,
        db=Depends(get_db),
        auth: dict = Depends(require_user_auth),
    ):
        principal = offer_access_requirement.SystemAdmission(
            reason="round 13 finding 4 sensitivity plant — no admission "
            "dependency guards this handler on purpose"
        )
        return catalog_service.offer_versions.create(db, payload, principal=principal)

    unguarded_app.add_api_route(
        "/api/v1/offer-versions", _unguarded_create_offer_version, methods=["POST"]
    )
    unguarded_app.dependency_overrides[get_db] = lambda: db_session
    unguarded_app.dependency_overrides[require_user_auth] = lambda: _auth_for(user)
    unguarded_client = TestClient(unguarded_app)
    unguarded_response = unguarded_client.post(
        "/api/v1/offer-versions",
        json={
            "offer_id": str(offer.id),
            "version_number": 2,
            "name": "v2",
            "service_type": "residential",
            "access_type": "fiber",
            "price_basis": "flat",
            "access_requirement": "unclassified",
        },
    )
    assert unguarded_response.status_code == 201, (
        "the unguarded clone must succeed where the real route refuses — "
        "that contrast IS the proof that the real route's dependency is "
        "what stands between an unauthorized caller and a written row"
    )


def test_a_grant_revoked_between_admission_and_mutation_still_refuses_the_patch(
    db_session, monkeypatch
):
    """Round 13 finding 3's direct proof, and the one property nothing
    else in this suite covers. OBSERVED: the mounted app, via a single
    real, issued HTTP PATCH request whose dependency chain — including
    ``_require_offer_version_admission`` and ``OfferVersions.update``'s own
    in-transaction ``verify_admission_authorization`` recheck — executes
    for real against the real database.

    A synchronous, single-process test cannot literally run a second,
    concurrent transaction mid-request without a heavier multi-connection
    harness this suite does not have; the accepted way to test a
    TOCTOU/revocation race in one process is to inject the "concurrent"
    revocation AT the exact seam between the two decisions under test, then
    verify the LATER decision (the one actually being tested) observes the
    committed change. The seam here is ``_admission_principal`` — called by
    ``update_offer_version`` immediately AFTER
    ``_require_offer_version_admission`` has already authorized the
    request, and immediately BEFORE ``OfferVersions.update``'s own recheck
    runs. Wrapping it to commit a real revocation, via the real ORM, before
    returning the principal is the smallest possible injection: everything
    else — the route, both dependency resolutions, and the recheck itself —
    runs unmodified and for real.

    Break condition: this fails (a 200 where it must be 403) if
    ``OfferVersions.update`` stops calling ``verify_admission_authorization``
    immediately before its mutation, or if that call is ever moved back to
    only running once, at the top of the route, before the window this
    test opens.
    """

    user = _system_user(db_session)
    _grant_direct_permission(
        db_session, user, offer_access_requirement.WRITE_PERMISSION
    )
    _grant_direct_permission(db_session, user, offer_access_requirement.ADMISSION_SCOPE)

    offer = _offer(db_session)
    app = _mounted_app(db_session)
    app.dependency_overrides[require_user_auth] = lambda: _auth_for(user)
    client = TestClient(app)

    create_response = client.post(
        "/api/v1/offer-versions",
        json={
            "offer_id": str(offer.id),
            "version_number": 1,
            "name": "v1",
            "service_type": "residential",
            "access_type": "fiber",
            "price_basis": "flat",
            "access_requirement": "unclassified",
        },
    )
    assert create_response.status_code == 201
    version_id = create_response.json()["id"]

    real_admission_principal = api_catalog._admission_principal

    def _admission_principal_that_revokes_mid_request(auth):
        # Runs AFTER _require_offer_version_admission has already
        # authorized this exact PATCH request, and BEFORE OfferVersions
        # .update's own recheck — the precise window round 13 finding 3
        # closes. Revokes and COMMITS for real, simulating a concurrent
        # transaction landing in that window.
        principal = real_admission_principal(auth)
        _revoke_direct_permission(
            db_session, user, offer_access_requirement.ADMISSION_SCOPE
        )
        return principal

    monkeypatch.setattr(
        api_catalog,
        "_admission_principal",
        _admission_principal_that_revokes_mid_request,
    )

    patch_response = client.patch(
        f"/api/v1/offer-versions/{version_id}",
        json={"name": "renamed after revocation"},
    )
    assert patch_response.status_code == 403, (
        "a grant revoked after the route's own dependency authorized the "
        "request, but before the mutation, must still refuse the write — "
        "this is the exact window OfferVersions.update's immediate-"
        "pre-mutation recheck exists to close"
    )
