from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.websocket import router


def test_staff_ping_uses_fresh_owner_session_and_bound_auth_session(
    monkeypatch,
) -> None:
    system_user_id = uuid4()
    auth_session_id = uuid4()
    captured: dict[str, object] = {}
    owner_db = object()

    @contextmanager
    def owner_command_session():
        captured["owner_session_opened"] = True
        yield owner_db

    monkeypatch.setattr(
        router.db_session_adapter,
        "owner_command_session",
        owner_command_session,
    )
    monkeypatch.setattr(
        router.team_inbox_assignment,
        "AgentPresenceHeartbeatCommand",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )

    def refresh(db, *, command):
        captured.update(db=db, command=command)
        return SimpleNamespace(
            disposition=(
                router.team_inbox_assignment.AgentPresenceHeartbeatDisposition.refreshed
            )
        )

    monkeypatch.setattr(
        router.team_inbox_assignment,
        "refresh_agent_presence",
        refresh,
    )
    observed_at = datetime(2026, 11, 3, 12, 30, tzinfo=UTC)

    assert router._refresh_staff_presence(
        {
            "principal_id": str(system_user_id),
            "principal_type": "system_user",
            "session_id": str(auth_session_id),
        },
        observed_at,
    )
    assert captured["owner_session_opened"] is True
    assert captured["db"] is owner_db
    command = captured["command"]
    assert command.system_user_id == system_user_id
    assert command.auth_session_id == auth_session_id
    assert command.observed_at == observed_at


def test_staff_ping_refuses_invalid_or_inactive_owner_outcome(monkeypatch) -> None:
    system_user_id = uuid4()
    auth_session_id = uuid4()

    @contextmanager
    def owner_command_session():
        yield object()

    monkeypatch.setattr(
        router.db_session_adapter,
        "owner_command_session",
        owner_command_session,
    )
    for disposition in (
        router.team_inbox_assignment.AgentPresenceHeartbeatDisposition.invalid_session,
        router.team_inbox_assignment.AgentPresenceHeartbeatDisposition.inactive_principal,
    ):
        monkeypatch.setattr(
            router.team_inbox_assignment,
            "refresh_agent_presence",
            lambda *_args, disposition=disposition, **_kwargs: SimpleNamespace(
                disposition=disposition
            ),
        )
        assert not router._refresh_staff_presence(
            {
                "principal_id": str(system_user_id),
                "principal_type": "system_user",
                "session_id": str(auth_session_id),
            },
            datetime(2026, 11, 3, 12, 30, tzinfo=UTC),
        )


def test_connect_and_disconnect_are_not_used_as_presence_evidence() -> None:
    source = Path("app/websocket/router.py").read_text(encoding="utf-8")

    assert "_refresh_staff_presence" in source
    assert source.count("_refresh_staff_presence(") == 1
    assert "authenticated WebSocket Inbox activity" in source
