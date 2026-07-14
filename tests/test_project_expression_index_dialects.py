from __future__ import annotations

from sqlalchemy import Index, create_mock_engine
from sqlalchemy.dialects import postgresql

from app.models.project import Project


def _quote_id_index() -> Index:
    return next(
        index
        for index in Project.__table__.indexes
        if index.name == "ix_projects_metadata_quote_id"
    )


def test_quote_id_expression_index_is_not_emitted_for_sqlite() -> None:
    emitted: list[object] = []
    engine = create_mock_engine(
        "sqlite://", lambda ddl, *args, **kwargs: emitted.append(ddl)
    )

    _quote_id_index().create(engine)

    assert emitted == []


def test_quote_id_expression_index_is_preserved_for_postgresql() -> None:
    emitted: list[object] = []
    engine = create_mock_engine(
        "postgresql+psycopg://",
        lambda ddl, *args, **kwargs: emitted.append(ddl),
    )

    _quote_id_index().create(engine)

    assert len(emitted) == 1
    sql = str(emitted[0].compile(dialect=postgresql.dialect()))
    assert "ix_projects_metadata_quote_id" in sql
    assert "metadata ->> 'quote_id'" in sql
    assert "WHERE is_active" in sql
