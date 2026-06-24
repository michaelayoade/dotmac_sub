"""Splynx MySQL connection helper for the historical-usage backfill.

Credentials come from the environment (SPLYNX_MYSQL_HOST/USER/PASS[/PORT/DB]).
Read-only against Splynx; the importers do all writes against the dotmac
PostgreSQL via the app's SessionLocal/engine.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager

import pymysql
import pymysql.cursors


@contextmanager
def splynx_connection(
    *, streaming: bool = False
) -> Generator[pymysql.Connection, None, None]:
    """Open a read-only connection to Splynx MySQL.

    ``streaming=True`` uses a server-side cursor (SSDictCursor) so a multi-
    million-row scan is not buffered into memory.
    """
    password = os.environ.get("SPLYNX_MYSQL_PASS") or os.environ.get(
        "SPLYNX_MYSQL_PASSWORD"
    )
    if not password:
        raise RuntimeError(
            "SPLYNX_MYSQL_PASS/SPLYNX_MYSQL_PASSWORD not set in the environment."
        )
    cursor_class = (
        pymysql.cursors.SSDictCursor if streaming else pymysql.cursors.DictCursor
    )
    conn = pymysql.connect(
        host=os.environ["SPLYNX_MYSQL_HOST"],
        port=int(os.environ.get("SPLYNX_MYSQL_PORT", "3306")),
        user=os.environ["SPLYNX_MYSQL_USER"],
        password=password,
        database=os.environ.get("SPLYNX_MYSQL_DB", "splynx"),
        cursorclass=cursor_class,
        connect_timeout=20,
        read_timeout=600,
        charset="utf8mb4",
    )
    try:
        yield conn
    finally:
        conn.close()
