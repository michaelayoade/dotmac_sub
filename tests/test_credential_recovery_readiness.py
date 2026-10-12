"""Synthetic SQLite policy evidence only; PostgreSQL rails are hosted evidence."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine

from app.services.credential_recovery import RecoveryReadinessReason
from app.services.credential_security_inventory import (
    CredentialInventoryQuery,
    RecoveryReadinessQuery,
    credential_inventory_statement,
    read_recovery_readiness,
    recovery_readiness_statement,
)
from app.services.domain_errors import DomainError
from scripts.authentication import credential_security_inventory as cli
from tests.test_credential_security_inventory import _readonly_db

QUERY = RecoveryReadinessQuery(observed_at=datetime(2026, 10, 12, tzinfo=UTC))


def test_mixed_principals_and_count_conservation() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.connection.driver_connection.create_function(
            "btrim",
            2,
            lambda value, chars: None if value is None else value.strip(chars),
        )
        conn.exec_driver_sql(
            "CREATE TABLE user_credentials (subscriber_id TEXT, system_user_id TEXT, "
            "reseller_user_id TEXT, provider TEXT, password_hash TEXT, "
            "password_updated_at DATETIME, last_login_at DATETIME, is_active BOOLEAN)"
        )
        for table in ("subscribers", "system_users", "reseller_users"):
            conn.exec_driver_sql(
                f"CREATE TABLE {table} (id TEXT, email TEXT, is_active BOOLEAN, status TEXT)"
            )
            for identifier, email, active, status in (
                ("eligible", "private@example.test", 1, "active"),
                ("reference", "ref@example.test", 1, "active"),
                ("oneway", "oneway@example.test", 1, "active"),
                ("suspended", "suspended@example.test", 1, "suspended"),
                ("blank", " \t\n\u2003", 1, "active"),
                ("empty", "", 1, "active"),
                ("null", None, 1, "active"),
                ("inactive", "inactive@example.test", 0, "active"),
                ("cancel", "cancel@example.test", 1, "canceled"),
                ("duplicate", "duplicate@example.test", 1, "active"),
            ):
                conn.exec_driver_sql(
                    f"INSERT INTO {table} VALUES (?, ?, ?, ?)",  # noqa: S608 - fixed table tuple
                    (identifier, email, active, status),
                )
        expected: dict[tuple[str, str, str], int] = {}

        def add(
            principal: str,
            identifier: str | None,
            reason: RecoveryReadinessReason,
            *,
            provider: str | None = "local",
            active: int = 1,
            stored: str = "plain:private",
            second_link: bool = False,
        ) -> None:
            links = [None, None, None]
            links[("customer", "staff", "reseller").index(principal)] = identifier
            if second_link:
                links[1] = "different"
            conn.exec_driver_sql(
                "INSERT INTO user_credentials VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (*links, provider, stored, None, None, active),
            )
            if stored.startswith(("plain:", "enc:", "bao://")):
                group = "invalid" if second_link or identifier is None else principal
                key = (
                    group,
                    "local" if provider == "local" else "external",
                    reason.value,
                )
                expected[key] = expected.get(key, 0) + 1

        for principal in ("customer", "staff", "reseller"):
            add(
                principal,
                "eligible",
                RecoveryReadinessReason.EXACT_LOCAL_EMAIL_ELIGIBLE,
            )
            add(
                principal,
                "reference",
                RecoveryReadinessReason.EXACT_LOCAL_EMAIL_ELIGIBLE,
                stored="bao://private",
            )
            add(principal, "oneway", RecoveryReadinessReason.AMBIGUOUS_BINDING)
            add(
                principal,
                "oneway",
                RecoveryReadinessReason.AMBIGUOUS_BINDING,
                stored="$argon2id$private",
            )
            for identifier in ("blank", "empty", "null"):
                add(principal, identifier, RecoveryReadinessReason.MISSING_EMAIL)
            add(principal, "inactive", RecoveryReadinessReason.INACTIVE)
            add(principal, "absent", RecoveryReadinessReason.MISSING_PRINCIPAL)
            add(
                principal, "eligible", RecoveryReadinessReason.NONLOCAL, provider="ldap"
            )
            add(principal, "eligible", RecoveryReadinessReason.INACTIVE, active=0)
            add(principal, "duplicate", RecoveryReadinessReason.AMBIGUOUS_BINDING)
            add(
                principal,
                "duplicate",
                RecoveryReadinessReason.AMBIGUOUS_BINDING,
                stored="enc:private",
            )
        add("customer", "eligible", RecoveryReadinessReason.NONLOCAL, provider=None)
        add("customer", "cancel", RecoveryReadinessReason.CANCELED)
        add("customer", "suspended", RecoveryReadinessReason.EXACT_LOCAL_EMAIL_ELIGIBLE)
        add("customer", None, RecoveryReadinessReason.MISSING_PRINCIPAL)
        add(
            "customer",
            "different",
            RecoveryReadinessReason.AMBIGUOUS_BINDING,
            second_link=True,
        )
        # A one-way verifier is not a reversible cohort or an active competing binding.
        add(
            "staff",
            "eligible",
            RecoveryReadinessReason.INACTIVE,
            active=0,
            stored="$argon2id$private",
        )
        rows = conn.execute(recovery_readiness_statement(query=QUERY)).all()
        census = conn.execute(
            credential_inventory_statement(
                query=CredentialInventoryQuery(QUERY.observed_at)
            )
        ).all()
    assert {(r[0], r[1], r[2]): r[3] for r in rows} == expected
    assert sum(r[3] for r in rows) == sum(
        r[6] for r in census if r[2] in ("plain", "encrypted", "secret_reference")
    )
    assert "private" not in str(rows)
    assert "eligible@example" not in str(rows)


@pytest.mark.parametrize(
    "invalid", ["new", "dirty", "deleted", "sqlite", "write", "isolation"]
)
def test_readiness_refuses_unsafe_session(invalid: str) -> None:
    db = _readonly_db()
    if invalid in ("new", "dirty", "deleted"):
        setattr(db, invalid, (object(),))
    elif invalid == "sqlite":
        db.get_bind.return_value.dialect.name = "sqlite"
    else:
        options = db.connection.return_value.get_execution_options.return_value
        options["postgresql_readonly" if invalid == "write" else "isolation_level"] = (
            False
        )
    with pytest.raises(DomainError, match="read-only snapshot"):
        read_recovery_readiness(db=db, query=QUERY)
    db.execute.assert_not_called()


def test_typed_readiness_is_count_only() -> None:
    db = _readonly_db()
    db.execute.return_value.all.return_value = [
        ("staff", "local", RecoveryReadinessReason.MISSING_EMAIL.value, 2)
    ]
    result = read_recovery_readiness(db=db, query=QUERY)
    assert result.reversible_count == 2
    assert result.observed_at == QUERY.observed_at
    output = cli.render_readiness(result=result)
    assert '"missing_or_blank_email_support_unresolved"' in output
    assert "safe_to_enforce" not in output
    for forbidden in ("password_hash", "username", "principal_id", "email_sha256"):
        assert forbidden not in output
    db.flush.assert_not_called()
    db.commit.assert_not_called()


def test_cli_readiness_uses_existing_snapshot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    from app.services.credential_security_inventory import RecoveryReadinessResult

    @contextmanager
    def snapshot() -> Iterator[MagicMock]:
        yield _readonly_db()

    def read(
        *, db: MagicMock, query: RecoveryReadinessQuery
    ) -> RecoveryReadinessResult:
        assert isinstance(query, RecoveryReadinessQuery)
        return RecoveryReadinessResult(observed_at=query.observed_at, cohorts=())

    monkeypatch.setattr(cli, "read_only_snapshot_session", snapshot)
    monkeypatch.setattr(cli, "read_recovery_readiness", read)
    assert cli.main(["--mode", "read-only", "--report", "recovery-readiness"]) == 0
    assert '"reversible_count": 0' in capsys.readouterr().out
