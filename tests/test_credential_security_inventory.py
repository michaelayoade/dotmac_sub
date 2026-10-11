"""Fast unit evidence only; SQLite does not prove deployed PostgreSQL rails."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from app.services.credential_security_inventory import (
    AgeBand,
    CredentialFormat,
    CredentialInventoryQuery,
    CredentialInventoryResult,
    credential_inventory_statement,
    read_credential_inventory,
)
from app.services.domain_errors import DomainError
from scripts.authentication import credential_security_inventory as cli

OBSERVED = datetime(2026, 10, 11, tzinfo=UTC)
QUERY = CredentialInventoryQuery(observed_at=OBSERVED)


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("$argon2id$malformed-private", CredentialFormat.ARGON2ID),
        ("$pbkdf2-sha256$malformed-private", CredentialFormat.PBKDF2_SHA256),
        *(
            (f"${variant}$private", CredentialFormat.BCRYPT)
            for variant in ("2a", "2b", "2y")
        ),
        ("$6$private", CredentialFormat.SHA512_CRYPT),
        ("plain:private", CredentialFormat.PLAIN),
        ("enc:invalid-private", CredentialFormat.ENCRYPTED),
        *(
            (f"{prefix}://private", CredentialFormat.SECRET_REFERENCE)
            for prefix in ("bao", "openbao", "vault", "env")
        ),
        ("$2x$private", CredentialFormat.UNKNOWN),
        ("raw-private", CredentialFormat.UNKNOWN),
        (None, CredentialFormat.MISSING),
        ("", CredentialFormat.MISSING),
    ],
)
def test_sql_returns_only_bounded_aggregates(
    stored: str | None, expected: CredentialFormat
):
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE user_credentials (subscriber_id TEXT, system_user_id TEXT, "
            "reseller_user_id TEXT, provider TEXT, password_hash TEXT, "
            "password_updated_at DATETIME, last_login_at DATETIME, is_active BOOLEAN)"
        )
        for _ in range(2):
            conn.exec_driver_sql(
                "INSERT INTO user_credentials VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("private-id", None, None, "local", stored, None, None, 1),
            )
        rows = conn.execute(credential_inventory_statement(query=QUERY)).all()
    assert len(rows) == 1
    assert tuple(rows[0]) == (
        "customer",
        "local",
        expected.value,
        "unknown",
        "unknown",
        True,
        2,
    )
    assert "private" not in str(rows)


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (None, AgeBand.UNKNOWN),
        (-1, AgeBand.FUTURE),
        (0, AgeBand.UNDER_30),
        (29, AgeBand.UNDER_30),
        (30, AgeBand.DAYS_30_89),
        (89, AgeBand.DAYS_30_89),
        (90, AgeBand.DAYS_90_364),
        (364, AgeBand.DAYS_90_364),
        (365, AgeBand.DAYS_365_PLUS),
    ],
)
def test_shared_utc_cutoffs(age: int | None, expected: AgeBand) -> None:
    from sqlalchemy import DateTime, literal, select

    from app.services.credential_security_inventory import _age_band

    observed = None if age is None else OBSERVED - timedelta(days=age)
    expression = _age_band(literal(observed, type_=DateTime(timezone=True)), OBSERVED)
    with create_engine("sqlite://").connect() as conn:
        assert conn.scalar(select(expression)) == expected.value


def _readonly_db() -> MagicMock:
    db = MagicMock()
    db.new = db.dirty = db.deleted = ()
    db.get_bind.return_value.dialect.name = "postgresql"
    db.connection.return_value.get_execution_options.return_value = {
        "postgresql_readonly": True,
        "isolation_level": "REPEATABLE READ",
    }
    return db


def test_typed_result_summaries_and_serialization() -> None:
    db = _readonly_db()
    db.execute.return_value.all.return_value = [
        ("staff", "local", "plain", "unknown", "unknown", True, 3),
        ("customer", "local", "bcrypt", "90_to_364_days", "unknown", True, 4),
        ("customer", "local", "bcrypt", "unknown", "under_30_days", True, 5),
        ("reseller", "external", "bcrypt", "unknown", "unknown", True, 6),
        ("staff", "local", "bcrypt", "unknown", "unknown", False, 7),
    ]
    result = read_credential_inventory(db=db, query=QUERY)
    assert result.total_count == 25
    assert result.reversible_count == 3
    assert result.dormant_legacy_count == 4
    output = cli.render_inventory(result=result)
    assert '"schema_version": 1' in output
    assert "password_hash" not in output
    assert "username" not in output
    db.commit.assert_not_called()
    db.flush.assert_not_called()


@pytest.mark.parametrize("invalid", ["dirty", "sqlite", "write", "isolation"])
def test_query_refuses_unsafe_session_before_aggregate(invalid: str) -> None:
    db = _readonly_db()
    if invalid == "dirty":
        db.dirty = (object(),)
    elif invalid == "sqlite":
        db.get_bind.return_value.dialect.name = "sqlite"
    else:
        options = db.connection.return_value.get_execution_options.return_value
        options["postgresql_readonly" if invalid == "write" else "isolation_level"] = (
            False
        )
    with pytest.raises(DomainError, match="read-only snapshot"):
        read_credential_inventory(db=db, query=QUERY)
    db.execute.assert_not_called()


def test_cli_requires_readonly_mode_before_opening_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = MagicMock()
    monkeypatch.setattr(cli, "read_only_snapshot_session", factory)
    for argv in ([], ["--mode", "apply"], ["--mode", "read-only", "--apply"]):
        with pytest.raises(SystemExit):
            cli.main(argv)
    factory.assert_not_called()


def test_cli_redacts_database_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    @contextmanager
    def snapshot() -> Iterator[MagicMock]:
        yield _readonly_db()

    def fail(
        *, db: MagicMock, query: CredentialInventoryQuery
    ) -> CredentialInventoryResult:
        raise SQLAlchemyError("plain:private username private-id token_hash")

    monkeypatch.setattr(cli, "read_only_snapshot_session", snapshot)
    monkeypatch.setattr(cli, "read_credential_inventory", fail)
    assert cli.main(["--mode", "read-only"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Credential inventory unavailable.\n"


def test_cli_success_uses_typed_query_and_snapshot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    @contextmanager
    def snapshot() -> Iterator[MagicMock]:
        yield _readonly_db()

    def read(
        *, db: MagicMock, query: CredentialInventoryQuery
    ) -> CredentialInventoryResult:
        assert query.observed_at.utcoffset() == timedelta(0)
        return CredentialInventoryResult(observed_at=query.observed_at, cohorts=())

    monkeypatch.setattr(cli, "read_only_snapshot_session", snapshot)
    monkeypatch.setattr(cli, "read_credential_inventory", read)
    assert cli.main(["--mode", "read-only"]) == 0
    assert '"total_count": 0' in capsys.readouterr().out


def test_query_requires_aware_observation() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        CredentialInventoryQuery(observed_at=datetime(2026, 10, 11))
