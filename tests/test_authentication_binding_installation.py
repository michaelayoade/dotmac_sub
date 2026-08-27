from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.models.audit import AuditActorType, AuditEvent
from app.models.auth import AuthenticationBinding
from app.services.credential_party_binding import (
    AUTHENTICATION_BINDING_INSTALL_SCOPE,
    AuthenticationBindingInstallation,
    AuthenticationBindingInstalled,
    CredentialBindingError,
    install_authentication_binding,
)
from app.services.owner_commands import CommandContext


def _command(
    *,
    binding_key: str = "oidc.field.test",
    mechanism_code: str = "oidc",
    name: str = "Field mobile OIDC",
    description: str | None = "Pinned field-mobile verifier",
) -> AuthenticationBindingInstallation:
    return AuthenticationBindingInstallation(
        context=CommandContext.system(
            actor="operator:test",
            scope=AUTHENTICATION_BINDING_INSTALL_SCOPE,
            reason="reviewed verifier installation",
            idempotency_key=f"authentication-binding:{binding_key}",
        ),
        binding_key=binding_key,
        mechanism_code=mechanism_code,
        name=name,
        description=description,
    )


def test_installation_commits_once_and_exact_replay_preserves_evidence(db_session):
    first = install_authentication_binding(db_session, _command())
    assert not db_session.in_transaction()

    replay = install_authentication_binding(db_session, _command())

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.binding_id == first.binding_id
    assert replay.installed_at == first.installed_at
    row = db_session.get(AuthenticationBinding, first.binding_id)
    assert row is not None
    assert row.mechanism_code == "oidc"
    audits = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "authentication_binding.installed")
        .all()
    )
    assert len(audits) == 1
    assert audits[0].actor_type is AuditActorType.system
    assert audits[0].actor_id == "operator:test"
    assert audits[0].metadata_["reason"] == "reviewed verifier installation"


def test_installation_refuses_key_reuse_with_different_reviewed_metadata(db_session):
    install_authentication_binding(db_session, _command())

    with pytest.raises(CredentialBindingError) as raised:
        install_authentication_binding(
            db_session,
            _command(description="different reviewed verifier"),
        )

    assert raised.value.code.endswith(".binding_configuration_conflict")


def test_installation_refuses_key_reuse_for_a_different_mechanism(db_session):
    install_authentication_binding(db_session, _command())

    with pytest.raises(CredentialBindingError) as raised:
        install_authentication_binding(
            db_session,
            _command(mechanism_code="radius"),
        )

    assert raised.value.code.endswith(".binding_identity_conflict")


def test_installation_refuses_exact_replay_of_a_retired_binding(db_session):
    installed = install_authentication_binding(db_session, _command())
    row = db_session.get(AuthenticationBinding, installed.binding_id)
    assert row is not None
    row.is_active = False
    db_session.commit()

    with pytest.raises(CredentialBindingError) as raised:
        install_authentication_binding(db_session, _command())

    assert raised.value.code.endswith(".authentication_binding_inactive")


def test_installation_refuses_an_undeclared_mechanism(db_session):
    with pytest.raises(CredentialBindingError) as raised:
        install_authentication_binding(
            db_session,
            _command(mechanism_code="invented"),
        )

    assert raised.value.code.endswith(".undeclared_mechanism")
    assert db_session.query(AuthenticationBinding).count() == 0


def test_unique_binding_key_is_the_concurrent_installation_arbiter() -> None:
    """Pin the savepoint shape that keeps a lost race from poisoning command state."""

    import inspect

    from app.services import credential_party_binding as owner

    source = inspect.getsource(owner._install_authentication_binding)
    assert "uq_auth_bindings_binding_key" in {
        constraint.name for constraint in AuthenticationBinding.__table__.constraints
    }
    assert "execute_owner_savepoint(db, insert)" in source
    assert "except IntegrityError:" in source
    assert "winner = existing_binding()" in source
    insert = source.index("def insert()")
    savepoint = source.index("execute_owner_savepoint(db, insert)")
    assert insert < source.index("db.add(binding)", insert) < savepoint


def test_cli_builds_the_typed_owner_command(monkeypatch, capsys):
    from scripts.authentication import install_authentication_binding as cli

    captured: dict[str, object] = {}
    fake_db = object()

    @contextmanager
    def owner_command_session():
        yield fake_db

    def install(db: object, command: AuthenticationBindingInstallation):
        captured["db"] = db
        captured["command"] = command
        return AuthenticationBindingInstalled(
            binding_id=UUID("e410d2d4-9777-4f16-b745-f80e65cd120c"),
            binding_key=command.binding_key,
            mechanism_code=command.mechanism_code,
            name=command.name,
            description=command.description,
            installed_at=datetime.now(UTC),
            replayed=False,
        )

    monkeypatch.setattr(
        cli,
        "db_session_adapter",
        SimpleNamespace(owner_command_session=owner_command_session),
    )
    monkeypatch.setattr(cli, "install_authentication_binding", install)

    result = cli.main(
        [
            "--binding-key",
            "oidc.field.primary",
            "--mechanism-code",
            "oidc",
            "--name",
            "Field mobile OIDC",
            "--actor",
            "operator:release",
            "--reason",
            "reviewed MOB-05 installation",
        ]
    )

    assert result == 0
    assert captured["db"] is fake_db
    command = captured["command"]
    assert isinstance(command, AuthenticationBindingInstallation)
    assert command.context.scope == AUTHENTICATION_BINDING_INSTALL_SCOPE
    assert command.context.actor == "operator:release"
    assert command.mechanism_code == "oidc"
    assert '"status": "installed"' in capsys.readouterr().out
