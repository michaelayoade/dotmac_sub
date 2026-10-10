"""ONT assignment picker clears an unselected subscriber without losing UUID checks."""

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_ont_assignments
from app.web.admin import network_onts_inventory

PICKER_PATH = "/admin/network/ont-assignment/subscriptions"


@pytest.fixture
def picker_client(db_session: Session) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(network_onts_inventory.router, prefix="/admin")

    def database() -> Session:
        return db_session

    def permission_granted() -> None:
        return None

    app.dependency_overrides[get_db] = database
    for route in network_onts_inventory.router.routes:
        if isinstance(route, APIRoute) and route.path.endswith(
            "/ont-assignment/subscriptions"
        ):
            for dependency in route.dependant.dependencies:
                if dependency.call is not None and dependency.call is not get_db:
                    app.dependency_overrides[dependency.call] = permission_granted

    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("query", ["", "?account_id="])
def test_unselected_subscriber_clears_options_without_querying_other_accounts(
    picker_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    def unexpected_read(
        db: Session, *, subscriber_id: UUID
    ) -> tuple[web_network_ont_assignments.AssignmentSubscriptionOption, ...]:
        raise AssertionError("an unselected subscriber must not query subscriptions")

    monkeypatch.setattr(
        web_network_ont_assignments, "assignment_subscription_options", unexpected_read
    )

    response = picker_client.get(PICKER_PATH + query)

    assert response.status_code == 200
    assert "Select a subscriber first" in response.text
    assert "disabled" in response.text
    assert '<option value="">Select a subscription</option>' not in response.text


def test_malformed_nonempty_subscriber_id_still_fails_validation(
    picker_client: TestClient,
) -> None:
    response = picker_client.get(PICKER_PATH, params={"account_id": "not-a-uuid"})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "account_id"]


def test_selected_subscriber_is_forwarded_as_exact_uuid(
    picker_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = uuid4()
    observed: list[UUID] = []

    def options_for_subscriber(
        db: Session, *, subscriber_id: UUID
    ) -> tuple[web_network_ont_assignments.AssignmentSubscriptionOption, ...]:
        assert db is db_session
        observed.append(subscriber_id)
        return ()

    monkeypatch.setattr(
        web_network_ont_assignments,
        "assignment_subscription_options",
        options_for_subscriber,
    )

    response = picker_client.get(PICKER_PATH, params={"account_id": str(selected)})

    assert response.status_code == 200
    assert observed == [selected]
    assert "No subscriptions found for this subscriber" in response.text


def test_unselected_subscriber_does_not_bypass_route_authentication() -> None:
    app = FastAPI()
    app.include_router(network_onts_inventory.router, prefix="/admin")

    with TestClient(app) as client:
        response = client.get(PICKER_PATH, params={"account_id": ""})

    assert response.status_code in {401, 403}
