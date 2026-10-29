"""The projection-secret adapter preserves authentication and owner boundaries."""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api import integrator_observations as port
from app.schemas.projection_secret_fetch import ProjectionSecretFetchRequest
from app.services.auth_dependencies import permission_requirement
from app.services.integrations import projection_secret_fetch as owner
from app.services.owner_commands import OwnerCommandError

PRINCIPAL = UUID("a7b2f210-c401-4bcd-925e-d23fe2dbd107")
CREDENTIAL = UUID("c25b9395-7833-4b22-966b-872731f7d674")
SECRET = "dummy-adapter-secret"


def _body() -> ProjectionSecretFetchRequest:
    return ProjectionSecretFetchRequest(
        credential_id=CREDENTIAL,
        secret_version=1,
        projection_request_ref="projection-request:dummy-adapter",
    )


def _auth() -> dict[str, object]:
    return {"principal_type": "api_key", "principal_id": str(PRINCIPAL)}


def _unexpected_session() -> None:
    pytest.fail("refusal must happen before opening the command session")


def test_disarmed_fetch_opens_no_command_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(port.settings, "network_provision_enabled", False)
    monkeypatch.setattr(
        port.db_session_adapter, "owner_command_session", _unexpected_session
    )
    with pytest.raises(HTTPException) as refusal:
        port.fetch_projection_secret(body=_body(), _auth=_auth())
    assert refusal.value.status_code == 409
    assert refusal.value.detail == "projection secret refused"


@pytest.mark.parametrize(
    "auth",
    [
        {"principal_type": "system_user", "principal_id": str(PRINCIPAL)},
        {"principal_type": "api_key", "principal_id": "not-a-principal"},
        {"principal_type": "api_key"},
    ],
)
def test_only_authenticated_machine_identity_reaches_the_owner(
    monkeypatch: pytest.MonkeyPatch, auth: dict[str, object]
) -> None:
    monkeypatch.setattr(port.settings, "network_provision_enabled", True)
    monkeypatch.setattr(
        port.db_session_adapter, "owner_command_session", _unexpected_session
    )
    with pytest.raises(HTTPException) as refusal:
        port.fetch_projection_secret(body=_body(), _auth=auth)
    assert refusal.value.status_code == 403


def test_adapter_supplies_typed_context_on_a_separate_command_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = object()
    lifecycle: list[str] = []

    @contextmanager
    def command_session() -> Iterator[Session]:
        lifecycle.append("opened")
        try:
            yield cast(Session, marker)
        finally:
            lifecycle.append("closed")

    def redeem(
        db: Session, *, command: owner.RedeemProjectionSecretCommand
    ) -> owner.ProjectionSecretRedeemed:
        assert db is marker
        assert command.context.actor == f"api_key:{PRINCIPAL}"
        assert command.context.scope == owner.PROJECTION_SECRET_FETCH_SCOPE
        assert command.context.command_id == command.context.correlation_id
        assert command.request_ref == _body().projection_request_ref
        assert command.credential_id == CREDENTIAL
        assert command.secret_version == 1
        lifecycle.append("committed-owner-result")
        return owner.ProjectionSecretRedeemed(secret=SECRET)

    monkeypatch.setattr(port.settings, "network_provision_enabled", True)
    monkeypatch.setattr(
        port.db_session_adapter, "owner_command_session", command_session
    )
    monkeypatch.setattr(owner, "redeem_projection_secret", redeem)

    response = port.fetch_projection_secret(body=_body(), _auth=_auth())

    assert lifecycle == ["opened", "committed-owner-result", "closed"]
    assert response.model_dump() == {"secret": SECRET}
    assert SECRET not in repr(response)


@pytest.mark.parametrize(
    ("failure", "status", "detail"),
    [
        (owner.SecretFetchRefused(), 409, "projection secret refused"),
        (
            OwnerCommandError(code="test.owner_boundary", message=SECRET),
            503,
            "projection secret unavailable",
        ),
    ],
)
def test_owner_failures_have_bounded_transport_responses(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
    status: int,
    detail: str,
) -> None:
    @contextmanager
    def command_session() -> Iterator[Session]:
        yield cast(Session, object())

    def redeem(
        db: Session, *, command: owner.RedeemProjectionSecretCommand
    ) -> owner.ProjectionSecretRedeemed:
        raise failure

    monkeypatch.setattr(port.settings, "network_provision_enabled", True)
    monkeypatch.setattr(
        port.db_session_adapter, "owner_command_session", command_session
    )
    monkeypatch.setattr(owner, "redeem_projection_secret", redeem)

    with pytest.raises(HTTPException) as refusal:
        port.fetch_projection_secret(body=_body(), _auth=_auth())
    assert refusal.value.status_code == status
    assert refusal.value.detail == detail
    assert refusal.value.__suppress_context__
    assert SECRET not in str(refusal.value)
    assert SECRET not in caplog.text


def test_fetch_retains_the_exact_declared_permission() -> None:
    dependency = (
        inspect.signature(port.fetch_projection_secret)
        .parameters["_auth"]
        .default.dependency
    )
    requirement = permission_requirement(dependency)
    assert requirement is not None
    assert requirement.write_any_of == (owner.PROJECTION_SECRET_FETCH_SCOPE,)
