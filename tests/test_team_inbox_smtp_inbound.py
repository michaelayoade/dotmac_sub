from __future__ import annotations

from collections.abc import Coroutine
from textwrap import dedent
from typing import Any

from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.team_inbox import InboxMessage, TeamInboxEmailRoute
from app.services import team_inbox_smtp_inbound


class _Envelope:
    def __init__(self, *, mail_from: str, rcpt_tos: list[str], content: bytes):
        self.mail_from = mail_from
        self.rcpt_tos = rcpt_tos
        self.content = content


def _run_immediate_coroutine(coroutine: Coroutine[Any, Any, str]) -> str:
    try:
        coroutine.send(None)
    except StopIteration as exc:
        return str(exc.value)
    coroutine.close()
    raise AssertionError("Expected handler coroutine to complete without awaiting.")


def _team(db_session, name: str, team_type: str) -> ServiceTeam:
    team = ServiceTeam(name=name, team_type=team_type)
    db_session.add(team)
    db_session.flush()
    return team


def _route(db_session, team: ServiceTeam, email: str) -> None:
    db_session.add(
        TeamInboxEmailRoute(
            service_team_id=team.id,
            email_address=email.lower(),
            is_active=True,
        )
    )
    db_session.flush()


def _raw_email(*, to: str = "support@dotmac.io", message_id: str = "msg") -> bytes:
    return (
        dedent(
            f"""\
            From: customer@example.com
            To: {to}
            Subject: SMTP inbound
            Message-ID: <{message_id}@example.com>
            Content-Type: text/plain; charset=utf-8

            Hello over SMTP.
            """
        )
        .replace("\n", "\r\n")
        .encode("utf-8")
    )


def test_handle_smtp_message_routes_allowed_recipient(db_session):
    support = _team(db_session, "Support", ServiceTeamType.support.value)
    _route(db_session, support, "support@dotmac.io")
    db_session.commit()

    result = team_inbox_smtp_inbound.handle_smtp_message(
        db_session,
        mail_from="customer@example.com",
        rcpt_to=["support@dotmac.io"],
        data=_raw_email(),
        allowed_recipients={"support@dotmac.io"},
    )

    message = db_session.get(InboxMessage, result.message_id)

    assert result.kind == "received"
    assert message.body == "Hello over SMTP."
    assert message.conversation.primary_service_team_id == support.id


def test_handle_smtp_message_skips_unmatched_recipient(db_session):
    result = team_inbox_smtp_inbound.handle_smtp_message(
        db_session,
        mail_from="customer@example.com",
        rcpt_to=["other@dotmac.io"],
        data=_raw_email(to="other@dotmac.io"),
        allowed_recipients={"support@dotmac.io"},
    )

    assert result.kind == "skipped"
    assert result.reason == "recipient_not_allowed"
    assert db_session.query(InboxMessage).count() == 0


def test_handle_smtp_message_skips_self_sender(db_session):
    result = team_inbox_smtp_inbound.handle_smtp_message(
        db_session,
        mail_from="support@dotmac.io",
        rcpt_to=["support@dotmac.io"],
        data=_raw_email(),
        allowed_recipients={"support@dotmac.io"},
    )

    assert result.kind == "skipped"
    assert result.reason == "self_sender"
    assert db_session.query(InboxMessage).count() == 0


def test_smtp_handler_returns_ok_for_accepted_message(db_session, monkeypatch):
    support = _team(db_session, "Support", ServiceTeamType.support.value)
    _route(db_session, support, "support@dotmac.io")
    db_session.commit()
    monkeypatch.setattr(
        team_inbox_smtp_inbound,
        "SessionLocal",
        lambda: db_session,
    )
    handler = team_inbox_smtp_inbound.TeamInboxSMTPHandler(
        allowed_recipients={"support@dotmac.io"}
    )

    response = _run_immediate_coroutine(
        handler.handle_DATA(
            None,
            None,
            _Envelope(
                mail_from="customer@example.com",
                rcpt_tos=["support@dotmac.io"],
                content=_raw_email(message_id="handler"),
            ),
        ),
    )

    assert response == "250 OK"
    assert db_session.query(InboxMessage).count() == 1
