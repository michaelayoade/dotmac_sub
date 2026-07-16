"""Database-authoritative external FreeRADIUS target resolution.

Active ``RadiusSyncJob`` + encrypted ``ConnectorConfig`` rows are the runtime
source of truth.  The legacy environment DSN is consumed only to bootstrap the
first target and to verify the cutover; it is never a runtime fallback.
"""

from __future__ import annotations

import hashlib
import re
import threading
from typing import Any

from sqlalchemy import Column, MetaData, String, Table, create_engine, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from app.models.connector import (
    ConnectorAuthType,
    ConnectorConfig,
    ConnectorType,
)
from app.models.radius import RadiusServer, RadiusSyncJob
from app.services import radius_dsn
from app.services.secrets import resolve_secret

_SQL_IDENT_PART_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EXTERNAL_ENGINES: dict[str, Engine] = {}
_EXTERNAL_ENGINES_LOCK = threading.Lock()


class ExternalRadiusTargetMismatch(RuntimeError):
    """The legacy env DSN and configured runtime target are not the same DB."""


def sanitize_table_identifier(raw: object, fallback: str) -> str:
    name = str(raw).strip() if raw is not None else fallback
    if not name:
        name = fallback
    parts = [part.strip().strip('"') for part in name.split(".")]
    if not all(_SQL_IDENT_PART_RE.fullmatch(part) for part in parts):
        raise ValueError(f"Invalid SQL table identifier: {name!r}")
    return ".".join(parts)


def external_radius_table(name: str, *columns: Column[Any]) -> Table:
    safe_name = sanitize_table_identifier(name, name)
    parts = safe_name.split(".")
    if len(parts) == 1:
        return Table(parts[0], MetaData(), *columns)
    return Table(parts[-1], MetaData(), *columns, schema=".".join(parts[:-1]))


def get_external_engine(db_url: str) -> Engine:
    engine = _EXTERNAL_ENGINES.get(db_url)
    if engine is not None:
        return engine
    with _EXTERNAL_ENGINES_LOCK:
        engine = _EXTERNAL_ENGINES.get(db_url)
        if engine is None:
            engine = create_engine(db_url, pool_pre_ping=True, pool_recycle=1800)
            _EXTERNAL_ENGINES[db_url] = engine
        return engine


def target_fingerprint(db_url: str) -> str:
    """Return a credential-free target identity suitable for logs/results."""
    url = make_url(db_url)
    host = (url.host or "local").lower()
    port = f":{url.port}" if url.port else ""
    database = (url.database or "").strip("/")
    public = f"{url.drivername}://{host}{port}/{database}"
    return hashlib.sha256(public.encode("utf-8")).hexdigest()[:16]


def _target_config(db: Session, job: RadiusSyncJob) -> dict[str, Any] | None:
    if not job.connector_config_id:
        return None
    connector = db.get(ConnectorConfig, job.connector_config_id)
    if not connector or connector.is_active is not True:
        return None
    auth_config = dict(connector.auth_config or {})
    metadata = dict(connector.metadata_ or {})
    db_url = auth_config.get("db_url") or connector.base_url
    if db_url:
        db_url = radius_dsn.container_safe_external_db_url(resolve_secret(db_url))
    if not db_url:
        driver = auth_config.get("driver") or "postgresql+psycopg"
        username = auth_config.get("username")
        password = resolve_secret(auth_config.get("password"))
        host = auth_config.get("host")
        port = auth_config.get("port")
        database = auth_config.get("database")
        if not all([username, password, host, database]):
            return None
        port_part = f":{port}" if port else ""
        db_url = radius_dsn.container_safe_external_db_url(
            f"{driver}://{username}:{password}@{host}{port_part}/{database}"
        )
    if not db_url:
        return None
    return {
        "target_id": str(job.id),
        "target_name": str(metadata.get("target_name") or job.name),
        "target_fingerprint": target_fingerprint(db_url),
        "db_url": db_url,
        "radcheck_table": sanitize_table_identifier(
            metadata.get("radcheck_table"), "radcheck"
        ),
        "radreply_table": sanitize_table_identifier(
            metadata.get("radreply_table"), "radreply"
        ),
        "radusergroup_table": sanitize_table_identifier(
            metadata.get("radusergroup_table"), "radusergroup"
        ),
        "radacct_table": sanitize_table_identifier(
            metadata.get("radacct_table"), "radacct"
        ),
        "nas_table": sanitize_table_identifier(metadata.get("nas_table"), "nas"),
        "radcheck_admin_table": sanitize_table_identifier(
            metadata.get("radcheck_admin_table"), "radcheck_admin"
        ),
        "radreply_admin_table": sanitize_table_identifier(
            metadata.get("radreply_admin_table"), "radreply_admin"
        ),
        "password_attribute": str(
            metadata.get("password_attribute") or "Cleartext-Password"
        ),
        "password_op": str(metadata.get("password_op") or ":="),
        "use_group": bool(metadata.get("use_group", False)),
        "group_priority": int(metadata.get("group_priority", 0)),
        "default_reply_op": str(metadata.get("default_reply_op") or ":="),
        "authoritative_accounting": bool(
            metadata.get("authoritative_accounting", False)
        ),
        "sync_users": bool(job.sync_users),
        "sync_nas_clients": bool(job.sync_nas_clients),
    }


def active_external_radius_targets(
    db: Session,
    *,
    capability: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve active DB-config targets; never fall back to the environment."""
    jobs = list(
        db.scalars(
            select(RadiusSyncJob)
            .where(RadiusSyncJob.is_active.is_(True))
            .where(RadiusSyncJob.connector_config_id.isnot(None))
            .order_by(RadiusSyncJob.created_at.asc(), RadiusSyncJob.id.asc())
        ).all()
    )
    configs: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for job in jobs:
        config = _target_config(db, job)
        if not config:
            continue
        if capability == "users" and not config["sync_users"]:
            continue
        if capability == "nas" and not config["sync_nas_clients"]:
            continue
        key = (
            config["db_url"],
            config["radcheck_table"],
            config["radreply_table"],
            config["radusergroup_table"],
            config["radacct_table"],
            config["nas_table"],
            config["radcheck_admin_table"],
            config["radreply_admin_table"],
        )
        if key in seen:
            continue
        seen.add(key)
        configs.append(config)
    return configs


def authoritative_accounting_target(db: Session) -> dict[str, Any] | None:
    targets = active_external_radius_targets(db)
    if not targets:
        return None
    selected = [target for target in targets if target["authoritative_accounting"]]
    if len(selected) == 1:
        return selected[0]
    if len(selected) > 1:
        raise RuntimeError(
            "Multiple external RADIUS targets are marked authoritative for accounting"
        )
    if len(targets) == 1:
        return targets[0]
    raise RuntimeError(
        "Multiple external RADIUS targets require exactly one "
        "authoritative_accounting target"
    )


def authoritative_external_radius_db_url(db: Session) -> str | None:
    target = authoritative_accounting_target(db)
    return str(target["db_url"]) if target else None


def seed_external_radius_target_from_env(db: Session) -> bool:
    """One-time bootstrap of DB configuration from the legacy environment DSN.

    Existing active DB configuration always wins and is never overwritten.
    The DSN is stored only in ``EncryptedJSON`` and is never logged.
    """
    if active_external_radius_targets(db):
        return False
    db_url = radius_dsn.resolve_radius_dsn()
    if not db_url:
        return False
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    auth_port = int(
        settings_spec.resolve_value(db, SettingDomain.radius, "default_auth_port")
        or 1812
    )
    acct_port = int(
        settings_spec.resolve_value(db, SettingDomain.radius, "default_acct_port")
        or 1813
    )
    url = make_url(db_url)
    host = (url.host or "radius-db").strip()
    server = db.scalars(
        select(RadiusServer)
        .where(RadiusServer.host == host)
        .where(RadiusServer.auth_port == auth_port)
        .where(RadiusServer.acct_port == acct_port)
    ).first()
    if server is None:
        server = RadiusServer(
            name="Bootstrapped external RADIUS",
            host=host,
            auth_port=auth_port,
            acct_port=acct_port,
            description="Bootstrapped from legacy environment DSN; DB config is authoritative",
            is_active=True,
        )
        db.add(server)
        db.flush()
    connector = db.scalars(
        select(ConnectorConfig).where(ConnectorConfig.name == "external-radius-primary")
    ).first()
    if connector is None:
        connector = ConnectorConfig(
            name="external-radius-primary",
            connector_type=ConnectorType.custom,
            auth_type=ConnectorAuthType.none,
            auth_config={"db_url": db_url},
            metadata_={
                "target_name": "primary",
                "authoritative_accounting": True,
                "legacy_env_bootstrap": True,
                "radcheck_table": "radcheck",
                "radreply_table": "radreply",
                "radusergroup_table": "radusergroup",
                "radacct_table": "radacct",
                "nas_table": "nas",
                "radcheck_admin_table": "radcheck_admin",
                "radreply_admin_table": "radreply_admin",
                "password_attribute": "Cleartext-Password",
                "password_op": ":=",
                "default_reply_op": ":=",
                "use_group": False,
                "group_priority": 0,
            },
            notes="Runtime RADIUS DB target; edit this row rather than environment DSNs",
            is_active=True,
        )
        db.add(connector)
        db.flush()
    job = db.scalars(
        select(RadiusSyncJob).where(RadiusSyncJob.connector_config_id == connector.id)
    ).first()
    if job is None:
        db.add(
            RadiusSyncJob(
                name="External RADIUS primary projection",
                server_id=server.id,
                connector_config_id=connector.id,
                sync_users=True,
                sync_nas_clients=True,
                is_active=True,
            )
        )
    db.commit()
    return True


def _database_identity(db_url: str) -> tuple[str, ...]:
    engine = get_external_engine(db_url)
    if engine.dialect.name == "sqlite":
        return ("sqlite", str(make_url(db_url).database or ""))
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT current_database(), COALESCE(inet_server_addr()::text, ''), "
                "COALESCE(inet_server_port(), 0)"
            )
        ).one()
    return (engine.dialect.name, str(row[0]), str(row[1]), str(row[2]))


def _table_signature(
    db_url: str,
    table_name: str,
    columns: tuple[str, ...],
) -> tuple[int, str]:
    """Credential-safe shadow signature; values are hashed and never returned."""
    table = external_radius_table(
        table_name, *(Column(column, String) for column in columns)
    )
    engine = get_external_engine(db_url)
    digest = hashlib.sha256()
    count = 0
    with engine.connect() as conn:
        rows = conn.execute(select(*(table.c[column] for column in columns))).all()
    for row in sorted(
        tuple("" if value is None else str(value) for value in row) for row in rows
    ):
        digest.update("\x1f".join(row).encode("utf-8"))
        digest.update(b"\x1e")
        count += 1
    return count, digest.hexdigest()


def shadow_verify_legacy_targets(db: Session) -> list[dict[str, Any]]:
    """Read-only cutover verification with no DSNs or credentials in output."""
    legacy_url = radius_dsn.resolve_radius_dsn()
    targets = active_external_radius_targets(db, capability="users")
    if not legacy_url or not targets:
        return []
    legacy_identity = _database_identity(legacy_url)
    legacy_tables = {
        "radcheck": ("radcheck", ("username", "attribute", "op", "value")),
        "radreply": ("radreply", ("username", "attribute", "op", "value")),
        "radusergroup": (
            "radusergroup",
            ("username", "groupname", "priority"),
        ),
    }
    results: list[dict[str, Any]] = []
    for target in targets:
        configured_identity = _database_identity(str(target["db_url"]))
        table_matches: dict[str, bool] = {}
        for key, (legacy_table, columns) in legacy_tables.items():
            configured_table = str(target[f"{key}_table"])
            if (
                configured_identity == legacy_identity
                and configured_table == legacy_table
            ):
                table_matches[key] = True
                continue
            table_matches[key] = _table_signature(
                legacy_url, legacy_table, columns
            ) == _table_signature(str(target["db_url"]), configured_table, columns)
        results.append(
            {
                "target_name": target["target_name"],
                "target_fingerprint": target["target_fingerprint"],
                "same_database": configured_identity == legacy_identity,
                "table_matches": table_matches,
                "verified": configured_identity == legacy_identity
                and all(table_matches.values()),
            }
        )
    return results


def assert_legacy_target_alignment(db: Session) -> list[dict[str, Any]]:
    results = shadow_verify_legacy_targets(db)
    mismatches = [result for result in results if not result["verified"]]
    if mismatches:
        names = ", ".join(str(result["target_name"]) for result in mismatches)
        raise ExternalRadiusTargetMismatch(
            "Configured external RADIUS target does not match the legacy env DSN: "
            f"{names}. Projection and CoA are blocked until cutover is verified."
        )
    return results
