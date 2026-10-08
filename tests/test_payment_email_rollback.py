"""Operational payment-email image rollback floor checks."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.services.domain_errors import DomainError
from app.services.payment_email_cutover import (
    RollbackCatalogObservation,
    legacy_image_rollback_allowed,
    read_legacy_image_rollback_floor,
)
from scripts import verify_payment_email_rollback

LEGACY = RollbackCatalogObservation(
    legacy_table=True,
    legacy_columns=5,
    cutover_relation=False,
    seal_column=False,
)


def test_only_complete_legacy_catalog_allows_old_image() -> None:
    assert legacy_image_rollback_allowed(LEGACY)
    for change in (
        {"legacy_table": False},
        {"legacy_columns": 4},
        {"cutover_relation": True},  # Empty or paused is still installed.
        {"seal_column": True},  # Independently blocks before activation.
    ):
        assert not legacy_image_rollback_allowed(replace(LEGACY, **change))


class _Rows:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row

    def one_or_none(self) -> tuple[object, ...] | None:
        return self.row


class _CatalogSession:
    def __init__(
        self,
        identity: tuple[object, ...] = ("dotmac_app", "dotmac_app", True, False, False),
        catalog: tuple[object, ...] | None = (True, 5, False, False),
        fail_catalog: bool = False,
    ) -> None:
        self.identity = identity
        self.catalog = catalog
        self.fail_catalog = fail_catalog
        self.queries: list[str] = []

    def get_bind(self) -> SimpleNamespace:
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def execute(self, statement: object) -> _Rows:
        query = str(statement)
        self.queries.append(query)
        if self.fail_catalog and len(self.queries) == 2:
            raise SQLAlchemyError("connection detail")
        return _Rows(self.identity if len(self.queries) == 1 else self.catalog)


def test_reader_uses_actual_runtime_identity_and_unfiltered_catalog() -> None:
    session = _CatalogSession()
    assert read_legacy_image_rollback_floor(session) == LEGACY  # type: ignore[arg-type]
    assert "session_user" in session.queries[0]
    assert "current_user" in session.queries[0]
    assert "pg_catalog.pg_class" in session.queries[1]
    assert "pg_catalog.pg_attribute" in session.queries[1]
    assert "information_schema" not in session.queries[1]


@pytest.mark.parametrize(
    "identity",
    [
        ("app_user", "postgres", True, False, False),
        ("postgres", "postgres", True, True, True),
        ("app_user", "app_user", True, True, False),
        ("app_user", "app_user", False, False, False),
    ],
)
def test_reader_refuses_role_drift_before_catalog_query(
    identity: tuple[object, ...],
) -> None:
    session = _CatalogSession(identity=identity)
    with pytest.raises(DomainError, match="rollback floor"):
        read_legacy_image_rollback_floor(session)  # type: ignore[arg-type]
    assert len(session.queries) == 1


@pytest.mark.parametrize(
    "catalog", [None, (True, None, False, False), (True, "5", False, False)]
)
def test_reader_refuses_incomplete_catalog(catalog: tuple[object, ...] | None) -> None:
    with pytest.raises(DomainError, match="rollback floor"):
        read_legacy_image_rollback_floor(_CatalogSession(catalog=catalog))  # type: ignore[arg-type]


def test_reader_refuses_catalog_query_failure_without_driver_detail() -> None:
    with pytest.raises(DomainError, match="rollback floor") as error:
        read_legacy_image_rollback_floor(_CatalogSession(fail_catalog=True))  # type: ignore[arg-type]
    assert "connection detail" not in str(error.value)


@pytest.mark.parametrize("installed", [False, True])
def test_cli_allows_only_legacy_floor_with_read_only_transaction(
    installed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    statements: list[str] = []

    class _Session:
        def __enter__(self) -> _Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def rollback(self) -> None:
            statements.append("ROLLBACK")

    monkeypatch.setattr(verify_payment_email_rollback, "SessionLocal", _Session)
    monkeypatch.setattr(
        verify_payment_email_rollback,
        "begin_read_only_snapshot",
        lambda _db: statements.append("READ_ONLY_SNAPSHOT_OPTIONS"),
    )
    monkeypatch.setattr(
        verify_payment_email_rollback,
        "read_legacy_image_rollback_floor",
        lambda _db: replace(LEGACY, cutover_relation=installed),
    )
    assert verify_payment_email_rollback.main([]) == (2 if installed else 0)
    assert statements == ["READ_ONLY_SNAPSHOT_OPTIONS", "ROLLBACK"]


def test_cli_refuses_query_failure_without_exposing_driver_detail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Session:
        def __enter__(self) -> _Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def fail_snapshot(_db: object) -> None:
        raise RuntimeError("private connection detail")

    monkeypatch.setattr(verify_payment_email_rollback, "SessionLocal", _Session)
    monkeypatch.setattr(
        verify_payment_email_rollback, "begin_read_only_snapshot", fail_snapshot
    )
    assert verify_payment_email_rollback.main([]) == 2
    assert "private connection detail" not in capsys.readouterr().err
