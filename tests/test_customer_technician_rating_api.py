"""Rating adapters preserve wire responses and canonical native-person policy."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api import me, reseller
from app.db import get_db
from app.models.audit import AuditEvent
from app.models.dispatch import WorkOrderAssignmentQueue
from app.models.subscriber import Reseller, ResellerUser
from app.models.vendor_routes import Vendor
from app.services.auth_dependencies import require_user_auth
from app.web.customer import work_orders as portal
from tests.test_customer_work_order_selfcare import _customer, _technician, _visit


def _client(module, db, principal):
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_user_auth] = lambda: principal
    return TestClient(app, headers={"Authorization": "Bearer synthetic-test"})


@pytest.mark.parametrize("surface", ["customer", "reseller"])
def test_rating_adapters_preserve_wire_and_map_native_policy(
    db_session, monkeypatch, surface
):
    customer, stranger = _customer(db_session), _customer(db_session)
    technician = _technician(db_session)
    native = _visit(db_session, customer, status="completed")
    unfinished = _visit(db_session, customer, status="in_progress")
    vendor_visit = _visit(db_session, customer, status="completed")
    foreign = _visit(db_session, stranger, status="completed")
    vendor = Vendor(name="Synthetic organization")
    db_session.add(vendor)
    db_session.flush()
    db_session.add_all(
        [
            WorkOrderAssignmentQueue(
                work_order_mirror_id=native.id,
                status="assigned",
                assigned_technician_id=technician.id,
            ),
            WorkOrderAssignmentQueue(
                work_order_mirror_id=vendor_visit.id,
                status="assigned",
                assigned_vendor_id=vendor.id,
            ),
        ]
    )
    partner = Reseller(name="Rating adapter reseller")
    db_session.add(partner)
    db_session.flush()
    actor = ResellerUser(reseller_id=partner.id, is_active=True)
    db_session.add(actor)
    customer.reseller_id = partner.id
    db_session.flush()
    principal_id, reseller_id = actor.id, partner.id
    db_session.commit()
    customer_id, native_id = customer.id, native.id
    if surface == "customer":
        client = _client(
            me,
            db_session,
            {"principal_type": "subscriber", "subscriber_id": str(customer_id)},
        )
        prefix = "/me/work-orders"
        actor = f"subscriber:{customer_id}"
    else:
        monkeypatch.setattr(
            reseller, "_reseller_id", lambda db, principal: str(reseller_id)
        )
        monkeypatch.setattr(
            reseller.reseller_portal,
            "owned_account",
            lambda db, reseller_id, account_id: SimpleNamespace(id=customer_id),
        )
        client = _client(
            reseller,
            db_session,
            {"principal_type": "reseller_user", "principal_id": str(principal_id)},
        )
        prefix = f"/reseller/accounts/{customer_id}/work-orders"
        actor = f"reseller:{reseller_id}:principal:{principal_id}"
    paths = {
        row.public_id: f"{prefix}/{row.public_id}/rate-technician"
        for row in (native, unfinished, vendor_visit, foreign)
    }
    first = client.post(paths[native.public_id], json={"rating": 5})
    assert first.status_code == 200
    assert first.json() == {
        "ok": True,
        "work_order_id": native.public_id,
        "rating": 5,
        "already_rated": False,
    }
    replay = client.post(paths[native.public_id], json={"rating": 1})
    assert replay.status_code == 200
    assert replay.json()["rating"] == 5
    assert replay.json()["already_rated"] is True
    assert (
        client.post(paths[unfinished.public_id], json={"rating": 5}).status_code == 409
    )
    assert (
        client.post(paths[vendor_visit.public_id], json={"rating": 5}).status_code
        == 409
    )
    assert client.post(paths[foreign.public_id], json={"rating": 5}).status_code == 404
    assert client.post(paths[native.public_id], json={"rating": 6}).status_code == 422
    audit = (
        db_session.query(AuditEvent)
        .filter_by(action="work_order_technician_rated", entity_id=str(native_id))
        .one()
    )
    assert audit.actor_id == actor


def test_api_and_portal_share_vendor_safe_person_projection(db_session, monkeypatch):
    customer = _customer(db_session)
    visit = _visit(db_session, customer, status="completed")
    visit.assigned_to_name = "Vendor organization"
    visit.technician_name = "Legacy arbitrary person"
    db_session.commit()
    principal = {"principal_type": "subscriber", "subscriber_id": str(customer.id)}
    api = me.my_work_orders(db=db_session, principal=principal)
    captured = {}
    monkeypatch.setattr(
        portal,
        "get_current_customer_from_request",
        lambda request, db: {"subscriber_id": str(customer.id)},
    )
    monkeypatch.setattr(
        portal,
        "optional_customer_subscriber_id",
        lambda db, customer_scope: str(customer.id),
    )

    def render(name, context):
        captured.update(context)
        return SimpleNamespace()

    monkeypatch.setattr(portal.templates, "TemplateResponse", render)
    portal.customer_work_orders(Request({"type": "http"}), db=db_session)
    assert api.work_orders[0].technician_name is None
    assert api.work_orders[0].technician_phone is None
    assert (
        api.work_orders[0].model_dump()
        == captured["tracker"].work_orders[0].model_dump()
    )
    assert not any(
        action.key == "rate_technician" for action in api.work_orders[0].actions
    )


@pytest.mark.parametrize("surface", ["customer", "reseller"])
def test_rating_cookie_csrf_and_native_bearer_use_existing_guard(
    db_session, monkeypatch, surface
):
    from app.csrf import require_browser_csrf_header
    from app.services import customer_work_order_selfcare as owner

    customer_id, reseller_id, principal_id = uuid4(), uuid4(), uuid4()
    calls = []

    def submit(db, *, command):
        calls.append(command)
        return owner.TechnicianRatingOutcome(
            command.work_order_public_id, command.rating
        )

    monkeypatch.setattr(owner, "rate_technician", submit)
    if surface == "customer":
        module = me
        principal = {"principal_type": "subscriber", "subscriber_id": str(customer_id)}
        path = "/me/work-orders/synthetic-rating/rate-technician"
    else:
        module = reseller
        principal = {
            "principal_type": "reseller_user",
            "principal_id": str(principal_id),
        }
        monkeypatch.setattr(
            reseller, "_reseller_id", lambda db, principal: str(reseller_id)
        )
        monkeypatch.setattr(
            reseller.reseller_portal,
            "owned_account",
            lambda *args: SimpleNamespace(id=customer_id),
        )
        path = f"/reseller/accounts/{customer_id}/work-orders/synthetic-rating/rate-technician"
    route = next(
        route
        for route in module.router.routes
        if route.path.endswith("/rate-technician")
    )
    assert any(
        dependency.call is require_browser_csrf_header
        for dependency in route.dependant.dependencies
    )
    client = _client(module, db_session, principal)
    client.headers.pop("authorization")
    client.cookies.set("csrf_token", "synthetic-csrf")
    assert client.post(path, json={"rating": 5}).status_code == 403
    assert (
        client.post(
            path, json={"rating": 5}, headers={"x-csrf-token": "mismatched"}
        ).status_code
        == 403
    )
    assert calls == []
    assert (
        client.post(
            path, json={"rating": 5}, headers={"x-csrf-token": "synthetic-csrf"}
        ).status_code
        == 200
    )
    client.cookies.clear()
    assert (
        client.post(
            path, json={"rating": 5}, headers={"Authorization": "Bearer synthetic-test"}
        ).status_code
        == 200
    )
    assert len(calls) == 2
    assert all(command.subscriber_id == customer_id for command in calls)
    if surface == "reseller":
        assert all(
            command.representative.reseller_id == reseller_id
            and command.representative.principal_id == principal_id
            for command in calls
        )
    else:
        assert all(command.representative is None for command in calls)


def test_reseller_portal_propagates_authenticated_representative_scope(
    db_session, monkeypatch
):
    from app.services import customer_work_order_selfcare as owner
    from app.services import web_reseller_routes as web

    customer_id, reseller_id, principal_id = uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(
        web,
        "_require_reseller_context",
        lambda request, db: {
            "reseller": SimpleNamespace(id=reseller_id),
            "principal_id": str(principal_id),
            "principal_type": "reseller_user",
        },
    )
    monkeypatch.setattr(
        web.reseller_portal,
        "owned_account",
        lambda *args: SimpleNamespace(id=customer_id),
    )
    commands = []

    def submit(db, *, command):
        commands.append(command)
        return owner.TechnicianRatingOutcome(
            command.work_order_public_id, command.rating
        )

    monkeypatch.setattr(owner, "rate_technician", submit)
    response = web.reseller_rate_technician(
        Request({"type": "http"}),
        db_session,
        str(customer_id),
        "synthetic-visit",
        5,
        "Synthetic feedback",
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("rated=ok")
    assert commands[0].representative == owner.ResellerRatingScope(
        reseller_id, principal_id, owner.RatingRepresentativeKind.reseller_user
    )
