"""Single authority for the bundled FreeRADIUS database DSN.

Both RADIUS writers — the ``radius_population`` sweep (raw psycopg) and the
event-time ``_external_sync_users`` path (SQLAlchemy) — must point at the SAME
radius database, or they split-brain: one becomes authoritative for some writes
and the other for the rest, silently, with no error. They historically resolved
the DSN through *different* precedence chains, so a stray ``RADIUS_SYNC_DB_URL``
(or an unset ``RADIUS_DB_DSN``) could split them. This module is the one place
that resolves it, so they cannot drift.

Precedence (highest first):
  1. ``RADIUS_SYNC_DB_URL``
  2. ``RADIUS_DB_DSN``
  3. constructed from ``RADIUS_DB_HOST`` / ``RADIUS_DB_PORT`` / ``RADIUS_DB_NAME``
     / ``RADIUS_DB_USER`` + ``RADIUS_DB_PASS`` (secret).

``resolve_radius_dsn()`` returns the canonical SQLAlchemy form
(``postgresql+psycopg://…``); ``radius_dsn_libpq()`` returns the same target in
libpq form (``postgresql://…``) for ``psycopg.connect()``.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

_SQLALCHEMY_PREFIX = "postgresql+psycopg://"
_LIBPQ_PREFIX = "postgresql://"


def normalize_external_db_url(value: str | None) -> str | None:
    """Canonicalize a DSN to the SQLAlchemy psycopg URL form."""
    if not value:
        return None
    db_url = value.strip()
    if not db_url:
        return None
    if db_url.startswith(_LIBPQ_PREFIX) and not db_url.startswith(_SQLALCHEMY_PREFIX):
        return _SQLALCHEMY_PREFIX + db_url[len(_LIBPQ_PREFIX) :]
    return db_url


def container_safe_external_db_url(value: str | None) -> str | None:
    """Rewrite a ``localhost``/``127.0.0.1`` DSN (on the default port) to the
    in-container radius host, so a host-oriented value still resolves from inside
    the Docker network. A non-default (host-mapped) port is kept as-is."""
    db_url = normalize_external_db_url(value)
    if not db_url:
        return None
    parsed = urlsplit(db_url)
    hostname = (parsed.hostname or "").strip().lower()
    if hostname not in {"localhost", "127.0.0.1"}:
        return db_url
    if parsed.port and parsed.port != 5432:
        return db_url
    host = (os.getenv("RADIUS_DB_HOST") or "radius-db").strip()
    port = os.getenv("RADIUS_DB_PORT") or "5432"
    username = parsed.username or ""
    password = parsed.password or ""
    auth = username
    if password:
        auth = f"{auth}:{password}"
    netloc = f"{auth}@{host}:{port}" if auth else f"{host}:{port}"
    return urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


def _from_parts() -> str | None:
    host = (os.getenv("RADIUS_DB_HOST") or "radius-db").strip()
    database = (os.getenv("RADIUS_DB_NAME") or "radius").strip()
    username = (os.getenv("RADIUS_DB_USER") or "radius").strip()
    from app.services.secrets import get_env_or_secret

    password = get_env_or_secret(
        "RADIUS_DB_PASS",
        "radius",
        "db_password",
        default="l2f3clS-Ws9WgTXcsW3HoznBnEq3n7N-",
    ).strip()
    if host and database and username and password:
        return f"{_SQLALCHEMY_PREFIX}{username}:{password}@{host}:5432/{database}"
    return None


def resolve_radius_dsn() -> str | None:
    """THE bundled FreeRADIUS DSN (SQLAlchemy form), or None if unconfigured."""
    return (
        container_safe_external_db_url(os.getenv("RADIUS_SYNC_DB_URL"))
        or container_safe_external_db_url(os.getenv("RADIUS_DB_DSN"))
        or _from_parts()
    )


def radius_dsn_libpq() -> str | None:
    """The same target as ``resolve_radius_dsn()`` in libpq form
    (``postgresql://…``), for ``psycopg.connect()``."""
    url = resolve_radius_dsn()
    if not url:
        return None
    if url.startswith(_SQLALCHEMY_PREFIX):
        return _LIBPQ_PREFIX + url[len(_SQLALCHEMY_PREFIX) :]
    return url
