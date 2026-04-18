"""Database connections for Splynx MySQL → DotMac Sub PostgreSQL migration."""

from __future__ import annotations

import logging
import os
from collections.abc import Generator
from contextlib import contextmanager

# Load .env for standalone script execution
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import pymysql
import pymysql.cursors
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sshtunnel import SSHTunnelForwarder

logger = logging.getLogger(__name__)

# --- Splynx MySQL connection ---
# Direct connection (preferred) or SSH tunnel fallback
SPLYNX_MYSQL_HOST = os.environ.get("SPLYNX_MYSQL_HOST", "")  # Direct connection host
SPLYNX_MYSQL_PORT = int(os.environ.get("SPLYNX_MYSQL_PORT", "3306"))
SPLYNX_MYSQL_DB = os.environ.get("SPLYNX_MYSQL_DB", "splynx")
SPLYNX_MYSQL_USER = os.environ.get("SPLYNX_MYSQL_USER", "migration")
SPLYNX_MYSQL_PASS = os.environ.get(
    "SPLYNX_MYSQL_PASS", os.environ.get("SPLYNX_MYSQL_PASSWORD", "")
)

# SSH tunnel settings (used when SPLYNX_MYSQL_HOST is not set)
SPLYNX_SSH_HOST = os.environ.get("SPLYNX_SSH_HOST", "138.68.165.175")
SPLYNX_SSH_USER = os.environ.get("SPLYNX_SSH_USER", "root")
SPLYNX_SSH_KEY = os.path.expanduser(
    os.environ.get("SPLYNX_SSH_KEY", "~/.ssh/id_ed25519")
)

# --- DotMac Sub PostgreSQL ---
DOTMAC_DATABASE_URL = os.environ.get("DATABASE_URL", "")


@contextmanager
def splynx_connection(
    dict_cursor: bool = True,
) -> Generator[pymysql.Connection, None, None]:
    """Open a connection to Splynx MySQL.

    Uses direct connection if SPLYNX_MYSQL_HOST is set, otherwise falls back
    to SSH tunnel.

    Usage::

        with splynx_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM customers LIMIT 5")
                rows = cur.fetchall()
    """
    if not SPLYNX_MYSQL_PASS:
        raise RuntimeError(
            "SPLYNX_MYSQL_PASS/SPLYNX_MYSQL_PASSWORD not set. "
            "Set it via environment or .env before opening a Splynx connection."
        )

    cursor_class = pymysql.cursors.DictCursor if dict_cursor else pymysql.cursors.Cursor

    # Direct connection if SPLYNX_MYSQL_HOST is set
    if SPLYNX_MYSQL_HOST:
        logger.info(
            "Connecting directly to Splynx MySQL at %s:%d",
            SPLYNX_MYSQL_HOST,
            SPLYNX_MYSQL_PORT,
        )
        conn = pymysql.connect(
            host=SPLYNX_MYSQL_HOST,
            port=SPLYNX_MYSQL_PORT,
            user=SPLYNX_MYSQL_USER,
            password=SPLYNX_MYSQL_PASS,
            database=SPLYNX_MYSQL_DB,
            charset="utf8mb4",
            cursorclass=cursor_class,
            connect_timeout=30,
            read_timeout=300,
        )
        try:
            yield conn
        finally:
            conn.close()
            logger.info("Splynx MySQL connection closed")
        return

    # Fall back to SSH tunnel
    tunnel = SSHTunnelForwarder(
        SPLYNX_SSH_HOST,
        ssh_username=SPLYNX_SSH_USER,
        ssh_pkey=SPLYNX_SSH_KEY,
        remote_bind_address=("127.0.0.1", 3306),
    )
    tunnel.start()
    logger.info(
        "SSH tunnel to %s open on local port %d",
        SPLYNX_SSH_HOST,
        tunnel.local_bind_port,
    )
    conn = pymysql.connect(
        host="127.0.0.1",
        port=tunnel.local_bind_port,
        user=SPLYNX_MYSQL_USER,
        password=SPLYNX_MYSQL_PASS,
        database=SPLYNX_MYSQL_DB,
        charset="utf8mb4",
        cursorclass=cursor_class,
        connect_timeout=30,
        read_timeout=300,
    )
    try:
        yield conn
    finally:
        conn.close()
        tunnel.stop()
        logger.info("SSH tunnel closed")


@contextmanager
def dotmac_session() -> Generator[Session, None, None]:
    """Open a SQLAlchemy session to DotMac Sub PostgreSQL.

    Usage::

        with dotmac_session() as db:
            subscriber = Subscriber(...)
            db.add(subscriber)
            db.commit()
    """
    engine = create_engine(DOTMAC_DATABASE_URL, echo=False)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def fetch_all(
    conn: pymysql.Connection,
    query: str,
    params: tuple | None = None,
) -> list[dict]:
    """Execute a query and return all rows as dicts."""
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def fetch_batched(
    conn: pymysql.Connection,
    query: str,
    batch_size: int = 1000,
    params: tuple | None = None,
) -> Generator[list[dict], None, None]:
    """Execute a query and yield rows in batches."""
    with conn.cursor() as cur:
        cur.execute(query, params)
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            yield rows
