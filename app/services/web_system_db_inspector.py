"""Read-only database inspector helpers."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import HTTPException
from sqlalchemy import MetaData, Table, func, select, text
from sqlalchemy.orm import Session

from app.models.auth import AuthProvider, UserCredential
from app.services.auth_flow import verify_password

MAX_ROWS = 500
DEFAULT_ROWS = 100
CONFIRM_TTL_SECONDS = 180
FORBIDDEN_SQL_PATTERN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|call|copy|vacuum|analyze|refresh|merge)\b",
    flags=re.IGNORECASE,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _secret() -> str:
    secret = os.getenv("JWT_SECRET")
    if not secret:
        raise RuntimeError(
            "JWT_SECRET environment variable is required for DB inspector"
        )
    return secret


def _normalize_query(query: str | None) -> str:
    text_query = (query or "").strip()
    if text_query.endswith(";"):
        text_query = text_query[:-1].strip()
    return text_query


def _strip_sql_comments(sql: str) -> str:
    """Remove SQL block comments and line comments before validation."""
    stripped = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    stripped = re.sub(r"--.*$", " ", stripped, flags=re.MULTILINE)
    return stripped


def validate_select_query(query: str | None) -> str:
    normalized = _normalize_query(query)
    if not normalized:
        raise HTTPException(status_code=400, detail="Query is required")

    comment_stripped = _strip_sql_comments(normalized)

    lowered = comment_stripped.strip().lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise HTTPException(status_code=400, detail="Only SELECT queries are allowed")

    if FORBIDDEN_SQL_PATTERN.search(comment_stripped):
        raise HTTPException(
            status_code=400, detail="Query contains forbidden statements"
        )

    if ";" in normalized:
        raise HTTPException(
            status_code=400, detail="Multiple statements are not allowed"
        )

    return normalized


def _safe_limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_ROWS
    return max(1, min(MAX_ROWS, int(value)))


def _run_wrapped_select(
    db: Session, query: str, *, row_limit: int
) -> tuple[list[str], list[dict[str, Any]], bool]:
    statement = text(
        f"SELECT * FROM ({query}) AS inspector_result LIMIT :row_limit"  # nosec  # noqa: S608
    )
    result = db.execute(statement, {"row_limit": row_limit + 1})
    rows = result.mappings().all()
    columns = list(result.keys())
    truncated = len(rows) > row_limit
    if truncated:
        rows = rows[:row_limit]
    return columns, [dict(row) for row in rows], truncated


def run_select_query(
    db: Session, query: str, *, row_limit: int | None = None
) -> dict[str, Any]:
    validated = validate_select_query(query)
    limit = _safe_limit(row_limit)
    columns, rows, truncated = _run_wrapped_select(db, validated, row_limit=limit)
    return {
        "query": validated,
        "row_limit": limit,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }


def _verify_local_password(db: Session, *, principal_id: str, password: str) -> bool:
    credential = db.scalars(
        select(UserCredential)
        .where(UserCredential.subscriber_id == principal_id)
        .where(UserCredential.provider == AuthProvider.local)
        .where(UserCredential.is_active.is_(True))
        .order_by(UserCredential.created_at.desc())
    ).first()
    if credential and verify_password(password, credential.password_hash):
        return True

    credential = db.scalars(
        select(UserCredential)
        .where(UserCredential.system_user_id == principal_id)
        .where(UserCredential.provider == AuthProvider.local)
        .where(UserCredential.is_active.is_(True))
        .order_by(UserCredential.created_at.desc())
    ).first()
    return bool(credential and verify_password(password, credential.password_hash))


def issue_confirmation_token(*, principal_id: str) -> str:
    issued = int(_now().timestamp())
    payload = f"{principal_id}:{issued}"
    signature = hmac.new(
        _secret().encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload}:{signature}"


def validate_confirmation_token(*, principal_id: str, token: str | None) -> bool:
    raw = (token or "").strip()
    if not raw:
        return False
    parts = raw.split(":")
    if len(parts) != 3:
        return False
    token_principal, issued_at_raw, signature = parts
    if token_principal != principal_id:
        return False
    payload = f"{token_principal}:{issued_at_raw}"
    expected = hmac.new(
        _secret().encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        issued_at = int(issued_at_raw)
    except ValueError:
        return False
    return (
        _now() - datetime.fromtimestamp(issued_at, tz=UTC)
    ).total_seconds() <= CONFIRM_TTL_SECONDS


def confirm_access(db: Session, *, principal_id: str, password: str) -> str:
    if not password or not password.strip():
        raise HTTPException(status_code=400, detail="Password is required")
    if not _verify_local_password(db, principal_id=principal_id, password=password):
        raise HTTPException(status_code=401, detail="Password confirmation failed")
    return issue_confirmation_token(principal_id=principal_id)


_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


def _table_name_regex() -> re.Pattern[str]:
    return _TABLE_NAME_RE


def _quote_identifier(name: str) -> str:
    """Quote a SQL identifier to prevent injection. Double any embedded quotes."""
    return '"' + name.replace('"', '""') + '"'


def _is_postgres(db: Session) -> bool:
    return db.bind is not None and db.bind.dialect.name == "postgresql"


def _table_schema(db: Session, table_name: str | None) -> list[dict[str, Any]]:
    target = (table_name or "").strip()
    if not target:
        return []
    if not _table_name_regex().match(target):
        return []

    if _is_postgres(db):
        if "." in target:
            schema, table = target.split(".", 1)
        else:
            schema, table = "public", target
        result = db.execute(
            text(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = :schema AND table_name = :table
                ORDER BY ordinal_position
                """
            ),
            {"schema": schema, "table": table},
        )
        return [dict(row) for row in result.mappings().all()]

    result = db.execute(text(f"PRAGMA table_info({_quote_identifier(target)})"))
    rows = []
    for row in result.mappings().all():
        rows.append(
            {
                "column_name": row.get("name"),
                "data_type": row.get("type"),
                "is_nullable": "NO" if row.get("notnull") else "YES",
            }
        )
    return rows


def _table_stats(db: Session) -> list[dict[str, Any]]:
    if _is_postgres(db):
        result = db.execute(
            text(
                """
                SELECT
                    schemaname || '.' || relname AS table_name,
                    COALESCE(n_live_tup, 0) AS row_count,
                    pg_total_relation_size((quote_ident(schemaname) || '.' || quote_ident(relname))::regclass) AS total_bytes,
                    seq_scan,
                    idx_scan
                FROM pg_stat_user_tables
                ORDER BY total_bytes DESC
                LIMIT 30
                """
            )
        )
        return [dict(row) for row in result.mappings().all()]

    tables = db.execute(
        text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    )
    rows: list[dict[str, Any]] = []
    for item in tables.mappings().all():
        table_name = str(item.get("name"))
        table = Table(table_name, MetaData())
        count_result = db.execute(select(func.count()).select_from(table))
        row_count = int(count_result.scalar() or 0)
        rows.append(
            {
                "table_name": table_name,
                "row_count": row_count,
                "total_bytes": None,
                "seq_scan": None,
                "idx_scan": None,
            }
        )
    return rows[:30]


def _index_usage(db: Session) -> list[dict[str, Any]]:
    if _is_postgres(db):
        result = db.execute(
            text(
                """
                SELECT
                    schemaname || '.' || relname AS table_name,
                    indexrelname AS index_name,
                    idx_scan,
                    idx_tup_read,
                    idx_tup_fetch
                FROM pg_stat_user_indexes
                ORDER BY idx_scan DESC, idx_tup_read DESC
                LIMIT 30
                """
            )
        )
        return [dict(row) for row in result.mappings().all()]
    return []


def _slow_query_summary(db: Session) -> tuple[list[dict[str, Any]], str | None]:
    if _is_postgres(db):
        try:
            result = db.execute(
                text(
                    """
                    SELECT
                        calls,
                        round((total_exec_time / calls)::numeric, 2) AS avg_ms,
                        round(total_exec_time::numeric, 2) AS total_ms,
                        left(query, 180) AS query
                    FROM pg_stat_statements
                    WHERE calls > 0
                    ORDER BY total_exec_time DESC
                    LIMIT 10
                    """
                )
            )
            return [dict(row) for row in result.mappings().all()], None
        except Exception as exc:
            logger.warning("pg_stat_statements query failed: %s", exc)
            return [], "pg_stat_statements is not available"
    return [], "Slow query summary is only available on PostgreSQL"


def build_overview(db: Session, *, selected_table: str | None) -> dict[str, Any]:
    slow_queries, slow_note = _slow_query_summary(db)
    return {
        "dialect": db.bind.dialect.name if db.bind is not None else "unknown",
        "table_stats": _table_stats(db),
        "index_usage": _index_usage(db),
        "slow_queries": slow_queries,
        "slow_query_note": slow_note,
        "table_schema": _table_schema(db, selected_table),
        "selected_table": (selected_table or "").strip(),
    }


def query_result_to_csv(result_payload: dict[str, Any]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    columns = [str(col) for col in (result_payload.get("columns") or [])]
    writer.writerow(columns)
    for row in result_payload.get("rows") or []:
        writer.writerow([row.get(col) for col in columns])
    return output.getvalue()
