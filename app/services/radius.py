import hashlib
import logging
import os
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException
from sqlalchemy import create_engine, or_, text
from sqlalchemy.orm import Session

from app.metrics import observe_job
from app.models.catalog import (
    AccessCredential,
    NasDevice,
    RadiusProfile,
    Subscription,
    SubscriptionStatus,
)
from app.models.connector import ConnectorConfig
from app.models.domain_settings import SettingDomain
from app.models.radius import (
    RadiusClient,
    RadiusServer,
    RadiusSyncJob,
    RadiusSyncRun,
    RadiusSyncStatus,
    RadiusUser,
)
from app.models.subscriber import Subscriber
from app.schemas.radius import (
    RadiusClientCreate,
    RadiusClientUpdate,
    RadiusServerCreate,
    RadiusServerUpdate,
    RadiusSyncJobCreate,
    RadiusSyncJobUpdate,
)
from app.services import settings_spec
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    validate_enum,
)
from app.services.credential_crypto import decrypt_credential, encrypt_credential
from app.services.response import ListResponseMixin
from app.services.secrets import resolve_secret

logger = logging.getLogger(__name__)


_CRYPT_PREFIXES = ("$1$", "$2a$", "$2b$", "$2y$", "$5$", "$6$")
RADIUS_SYNC_ELIGIBLE_STATUSES = (
    SubscriptionStatus.active,
    SubscriptionStatus.suspended,
    SubscriptionStatus.canceled,
    SubscriptionStatus.expired,
)
_OPAQUE_RADIUS_VALUE_RE = re.compile(r"^[A-Za-z0-9+/=]+$")


def _external_password_row(
    credential: AccessCredential,
    *,
    default_attribute: str,
    default_op: str,
) -> tuple[str, str, str] | None:
    secret_hash = str(credential.secret_hash or "").strip()
    if not secret_hash:
        return None
    lowered = secret_hash.lower()
    if lowered.startswith(("plain:", "cleartext:", "enc:")):
        return ("Cleartext-Password", ":=", decrypt_credential(secret_hash) or "")
    if secret_hash.startswith(_CRYPT_PREFIXES):
        return ("Crypt-Password", ":=", secret_hash)
    if secret_hash.startswith("$pbkdf2-"):
        logger.warning(
            "Skipping external RADIUS password sync for %s: unsupported legacy PBKDF2 service secret",
            credential.username,
        )
        return None
    # Detect base64-encoded hashes from migration (no prefix, not crypt-style).
    # These cannot be used as Cleartext-Password — they will cause auth failures.
    if len(secret_hash) >= 20 and secret_hash.endswith("="):
        logger.warning(
            "Skipping external RADIUS password sync for %s: "
            "opaque hash detected (likely migration artifact, not cleartext)",
            credential.username,
        )
        return None
    return (default_attribute, default_op, secret_hash)


def _radius_sync_subscription_for_subscriber(
    db: Session, subscriber_id
) -> Subscription | None:
    return (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == subscriber_id)
        .filter(Subscription.status.in_(RADIUS_SYNC_ELIGIBLE_STATUSES))
        .order_by(
            Subscription.start_at.desc().nullslast(),
            Subscription.created_at.desc(),
        )
        .first()
    )


def _coerce_int_setting(value: object) -> int | None:
    # settings_spec.resolve_value() is intentionally loose-typed (object).
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None
    return None


def _is_opaque_radius_password(value: str | None) -> bool:
    text = str(value or "").strip()
    if len(text) < 20:
        return False
    if not _OPAQUE_RADIUS_VALUE_RE.fullmatch(text):
        return False
    return any(ch in text for ch in "+/=")


def _normalize_imported_radius_secret(
    attribute: str | None,
    value: str | None,
) -> tuple[str | None, bool]:
    attr = str(attribute or "").strip().lower()
    raw_value = str(value or "").strip()
    if not raw_value:
        return None, False
    if attr == "crypt-password":
        return raw_value, True
    if attr == "cleartext-password":
        if _is_opaque_radius_password(raw_value):
            return None, False
        return encrypt_credential(raw_value), True
    return None, False


def _dedupe_single_subscriber(rows: list[Subscriber]) -> list[Subscriber]:
    deduped: dict[str, Subscriber] = {}
    for row in rows:
        deduped[str(row.id)] = row
    return list(deduped.values())


def _latest_sync_eligible_subscription_for_subscriber(
    db: Session, subscriber_id: Any
) -> Subscription | None:
    return (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == subscriber_id)
        .filter(Subscription.status.in_(RADIUS_SYNC_ELIGIBLE_STATUSES))
        .order_by(
            Subscription.start_at.desc().nullslast(),
            Subscription.created_at.desc(),
        )
        .first()
    )


def _read_external_radius_credentials(config: dict) -> list[dict[str, str]]:
    radcheck = config["radcheck_table"]
    engine = create_engine(config["db_url"])
    query = text(
        f"""
        SELECT username, attribute, op, value
        FROM {radcheck}
        WHERE lower(attribute) IN ('cleartext-password', 'crypt-password')
        ORDER BY username
        """  # noqa: S608 — radcheck is from admin settings, not user input
    )
    selected: dict[str, dict[str, str]] = {}
    priority = {"cleartext-password": 2, "crypt-password": 1}
    with engine.begin() as conn:
        for row in conn.execute(query):
            username = str(row.username or "").strip()
            if not username:
                continue
            attribute = str(row.attribute or "").strip()
            current = selected.get(username)
            current_priority = priority.get(str(current.get("attribute") or "").lower(), 0) if current else 0
            new_priority = priority.get(attribute.lower(), 0)
            if current is None or new_priority > current_priority:
                selected[username] = {
                    "username": username,
                    "attribute": attribute,
                    "op": str(row.op or "").strip(),
                    "value": str(row.value or "").strip(),
                }
    return list(selected.values())


def import_access_credentials_from_external_radius(
    db: Session,
    *,
    config: dict | None = None,
) -> dict[str, Any]:
    external_config = config or _bundled_external_db_config()
    if not external_config:
        raise ValueError("No external RADIUS database configuration is available.")

    imported_rows = _read_external_radius_credentials(external_config)

    existing_credentials = db.query(AccessCredential).all()
    existing_by_username = {
        str(credential.username).strip().lower(): credential
        for credential in existing_credentials
        if getattr(credential, "username", None)
    }

    subscriptions = (
        db.query(Subscription)
        .filter(Subscription.login.isnot(None))
        .order_by(Subscription.created_at.desc())
        .all()
    )
    subscriptions_by_login: dict[str, list[Subscriber]] = {}
    for subscription in subscriptions:
        login = str(subscription.login or "").strip().lower()
        if not login:
            continue
        linked_subscriber = db.get(Subscriber, subscription.subscriber_id)
        if linked_subscriber and linked_subscriber.is_active:
            subscriptions_by_login.setdefault(login, []).append(linked_subscriber)

    subscribers = (
        db.query(Subscriber)
        .filter(Subscriber.is_active.is_(True))
        .all()
    )
    subscribers_by_number: dict[str, list[Subscriber]] = {}
    subscribers_by_account_number: dict[str, list[Subscriber]] = {}
    for subscriber_record in subscribers:
        subscriber_number = str(subscriber_record.subscriber_number or "").strip().lower()
        if subscriber_number:
            subscribers_by_number.setdefault(subscriber_number, []).append(subscriber_record)
        account_number = str(subscriber_record.account_number or "").strip().lower()
        if account_number:
            subscribers_by_account_number.setdefault(account_number, []).append(subscriber_record)

    created = 0
    updated = 0
    reactivated = 0
    secrets_imported = 0
    secrets_skipped = 0
    matched_existing_credential = 0
    matched_subscription_login = 0
    matched_subscriber_number = 0
    matched_account_number = 0
    unmatched: list[str] = []
    conflicts: list[str] = []

    for row in imported_rows:
        username = str(row["username"]).strip()
        username_key = username.lower()
        credential = existing_by_username.get(username_key)
        subscriber: Subscriber | None = None
        match_source = "none"

        if credential is not None:
            subscriber = db.get(Subscriber, credential.subscriber_id)
            match_source = "existing_credential"
            matched_existing_credential += 1
        else:
            login_matches = _dedupe_single_subscriber(
                subscriptions_by_login.get(username_key, [])
            )
            if len(login_matches) == 1:
                subscriber = login_matches[0]
                match_source = "subscription_login"
                matched_subscription_login += 1
            elif len(login_matches) > 1:
                conflicts.append(username)
                continue
            else:
                number_matches = _dedupe_single_subscriber(
                    subscribers_by_number.get(username_key, [])
                )
                if len(number_matches) == 1:
                    subscriber = number_matches[0]
                    match_source = "subscriber_number"
                    matched_subscriber_number += 1
                elif len(number_matches) > 1:
                    conflicts.append(username)
                    continue
                else:
                    account_matches = _dedupe_single_subscriber(
                        subscribers_by_account_number.get(username_key, [])
                    )
                    if len(account_matches) == 1:
                        subscriber = account_matches[0]
                        match_source = "account_number"
                        matched_account_number += 1
                    elif len(account_matches) > 1:
                        conflicts.append(username)
                        continue

        if subscriber is None:
            unmatched.append(username)
            continue

        imported_secret, secret_usable = _normalize_imported_radius_secret(
            row.get("attribute"),
            row.get("value"),
        )
        if secret_usable:
            secrets_imported += 1
        else:
            secrets_skipped += 1

        if credential is None:
            credential = AccessCredential(
                subscriber_id=subscriber.id,
                username=username,
                secret_hash=imported_secret,
                is_active=True,
            )
            db.add(credential)
            db.flush()
            existing_by_username[username_key] = credential
            created += 1
        else:
            changed = False
            if credential.subscriber_id != subscriber.id:
                conflicts.append(username)
                continue
            if credential.username != username:
                credential.username = username
                changed = True
            if credential.is_active is not True:
                credential.is_active = True
                reactivated += 1
                changed = True
            if imported_secret and credential.secret_hash != imported_secret:
                credential.secret_hash = imported_secret
                changed = True
            if changed:
                updated += 1

        if match_source in {"subscriber_number", "account_number"}:
            matched_subscription: Subscription | None = (
                _latest_sync_eligible_subscription_for_subscriber(
                db, subscriber.id
            )
            )
            if matched_subscription and not str(matched_subscription.login or "").strip():
                matched_subscription.login = username

    db.commit()

    return {
        "scanned": len(imported_rows),
        "created": created,
        "updated": updated,
        "reactivated": reactivated,
        "matched_existing_credential": matched_existing_credential,
        "matched_subscription_login": matched_subscription_login,
        "matched_subscriber_number": matched_subscriber_number,
        "matched_account_number": matched_account_number,
        "secrets_imported": secrets_imported,
        "secrets_skipped": secrets_skipped,
        "unmatched": len(unmatched),
        "conflicts": len(conflicts),
        "unmatched_examples": unmatched[:10],
        "conflict_examples": conflicts[:10],
    }


class RadiusServers(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: RadiusServerCreate):
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "auth_port" not in fields_set:
            default_auth_port = settings_spec.resolve_value(
                db, SettingDomain.radius, "default_auth_port"
            )
            auth_port = _coerce_int_setting(default_auth_port)
            if auth_port is not None:
                data["auth_port"] = auth_port
        if "acct_port" not in fields_set:
            default_acct_port = settings_spec.resolve_value(
                db, SettingDomain.radius, "default_acct_port"
            )
            acct_port = _coerce_int_setting(default_acct_port)
            if acct_port is not None:
                data["acct_port"] = acct_port
        server = RadiusServer(**data)
        db.add(server)
        db.commit()
        db.refresh(server)
        return server

    @staticmethod
    def get(db: Session, server_id: str):
        server = db.get(RadiusServer, server_id)
        if not server:
            raise HTTPException(status_code=404, detail="Radius server not found")
        return server

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(RadiusServer)
        if is_active is None:
            query = query.filter(RadiusServer.is_active.is_(True))
        else:
            query = query.filter(RadiusServer.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": RadiusServer.created_at, "name": RadiusServer.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, server_id: str, payload: RadiusServerUpdate):
        server = db.get(RadiusServer, server_id)
        if not server:
            raise HTTPException(status_code=404, detail="Radius server not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(server, key, value)
        db.commit()
        db.refresh(server)
        return server

    @staticmethod
    def delete(db: Session, server_id: str):
        server = db.get(RadiusServer, server_id)
        if not server:
            raise HTTPException(status_code=404, detail="Radius server not found")
        server.is_active = False
        db.commit()


class RadiusClients(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: RadiusClientCreate):
        client = RadiusClient(**payload.model_dump())
        db.add(client)
        db.commit()
        db.refresh(client)
        return client

    @staticmethod
    def get(db: Session, client_id: str):
        client = db.get(RadiusClient, client_id)
        if not client:
            raise HTTPException(status_code=404, detail="Radius client not found")
        return client

    @staticmethod
    def list(
        db: Session,
        server_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(RadiusClient)
        if server_id:
            query = query.filter(RadiusClient.server_id == server_id)
        if is_active is None:
            query = query.filter(RadiusClient.is_active.is_(True))
        else:
            query = query.filter(RadiusClient.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": RadiusClient.created_at, "client_ip": RadiusClient.client_ip},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, client_id: str, payload: RadiusClientUpdate):
        client = db.get(RadiusClient, client_id)
        if not client:
            raise HTTPException(status_code=404, detail="Radius client not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(client, key, value)
        db.commit()
        db.refresh(client)
        return client

    @staticmethod
    def delete(db: Session, client_id: str):
        client = db.get(RadiusClient, client_id)
        if not client:
            raise HTTPException(status_code=404, detail="Radius client not found")
        client.is_active = False
        db.commit()


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _radius_client_ip_for_nas(nas_device: NasDevice) -> str:
    return (nas_device.nas_ip or nas_device.management_ip or nas_device.ip_address or "").strip()


def _active_radius_servers(db: Session) -> list[RadiusServer]:
    return (
        db.query(RadiusServer)
        .filter(RadiusServer.is_active.is_(True))
        .order_by(RadiusServer.created_at.asc())
        .all()
    )


def _normalize_external_db_url(value: str | None) -> str | None:
    if not value:
        return None
    db_url = value.strip()
    if not db_url:
        return None
    if db_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + db_url[len("postgresql://") :]
    return db_url


def _container_safe_external_db_url(value: str | None) -> str | None:
    db_url = _normalize_external_db_url(value)
    if not db_url:
        return None
    parsed = urlsplit(db_url)
    hostname = (parsed.hostname or "").strip().lower()
    if hostname not in {"localhost", "127.0.0.1"}:
        return db_url

    # If the URL already uses a non-default port (host-mapped), keep it as-is.
    # Only rewrite to Docker hostname when port is the default 5432.
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
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _bundled_external_db_config() -> dict | None:
    """Fallback external RADIUS DB config for the bundled Docker stack."""
    db_url = _container_safe_external_db_url(os.getenv("RADIUS_SYNC_DB_URL"))
    if not db_url:
        db_url = _container_safe_external_db_url(os.getenv("RADIUS_DB_DSN"))
    if not db_url:
        host = (os.getenv("RADIUS_DB_HOST") or "radius-db").strip()
        database = (os.getenv("RADIUS_DB_NAME") or "radius").strip()
        username = (os.getenv("RADIUS_DB_USER") or "radius").strip()
        password = (os.getenv("RADIUS_DB_PASS") or "l2f3clS-Ws9WgTXcsW3HoznBnEq3n7N-").strip()
        if host and database and username and password:
            db_url = f"postgresql+psycopg://{username}:{password}@{host}:5432/{database}"
    if not db_url:
        return None
    return {
        "db_url": db_url,
        "radcheck_table": '"radcheck"',
        "radreply_table": '"radreply"',
        "radusergroup_table": '"radusergroup"',
        "nas_table": '"nas"',
        "password_attribute": "Cleartext-Password",
        "password_op": ":=",
        "use_group": False,
        "group_priority": 0,
        "default_reply_op": ":=",
    }


def _active_external_sync_configs(db: Session) -> list[dict]:
    configs: list[dict] = []
    jobs = (
        db.query(RadiusSyncJob)
        .filter(RadiusSyncJob.is_active.is_(True))
        .filter(RadiusSyncJob.connector_config_id.isnot(None))
        .all()
    )
    for job in jobs:
        config = _external_db_config(db, job)
        if config:
            configs.append(config)
    if configs:
        return configs
    fallback = _bundled_external_db_config()
    return [fallback] if fallback else []


def ensure_radius_clients_for_nas(db: Session, nas_device: NasDevice) -> int:
    """Ensure active RadiusClient rows exist for a NAS on all active servers."""
    client_ip = _radius_client_ip_for_nas(nas_device)
    if not client_ip or not nas_device.shared_secret:
        return 0

    decrypted_secret = decrypt_credential(nas_device.shared_secret)
    raw_secret = resolve_secret(decrypted_secret)
    if not raw_secret:
        return 0

    servers = _active_radius_servers(db)
    if not servers:
        return 0

    secret_hash = _hash_secret(raw_secret)
    changed = 0
    for server in servers:
        existing = (
            db.query(RadiusClient)
            .filter(RadiusClient.server_id == server.id)
            .filter(
                or_(
                    RadiusClient.nas_device_id == nas_device.id,
                    RadiusClient.client_ip == client_ip,
                )
            )
            .first()
        )
        if existing:
            updated = False
            if existing.nas_device_id != nas_device.id:
                existing.nas_device_id = nas_device.id
                updated = True
            if existing.client_ip != client_ip:
                existing.client_ip = client_ip
                updated = True
            if existing.shared_secret_hash != secret_hash:
                existing.shared_secret_hash = secret_hash
                updated = True
            if existing.description != nas_device.name:
                existing.description = nas_device.name
                updated = True
            if existing.is_active is not True:
                existing.is_active = True
                updated = True
            if updated:
                changed += 1
            continue

        db.add(
            RadiusClient(
                server_id=server.id,
                nas_device_id=nas_device.id,
                client_ip=client_ip,
                shared_secret_hash=secret_hash,
                description=nas_device.name,
                is_active=True,
            )
        )
        changed += 1
    return changed


def ensure_radius_users_for_subscription(db: Session, subscription: Subscription) -> int:
    """Ensure internal RadiusUser rows exist for active credentials on a subscription."""
    if subscription.status != SubscriptionStatus.active:
        return 0

    credentials = (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == subscription.subscriber_id)
        .filter(AccessCredential.is_active.is_(True))
        .order_by(AccessCredential.updated_at.desc(), AccessCredential.created_at.desc())
        .all()
    )
    if not credentials:
        return 0

    changed = 0
    for credential in credentials:
        existing_user = (
            db.query(RadiusUser)
            .filter(RadiusUser.access_credential_id == credential.id)
            .first()
        )
        profile_id = credential.radius_profile_id or subscription.radius_profile_id
        if existing_user:
            updated = False
            if existing_user.subscription_id != subscription.id:
                existing_user.subscription_id = subscription.id
                updated = True
            if existing_user.subscriber_id != subscription.subscriber_id:
                existing_user.subscriber_id = subscription.subscriber_id
                updated = True
            if existing_user.username != credential.username:
                existing_user.username = credential.username
                updated = True
            if existing_user.secret_hash != credential.secret_hash:
                existing_user.secret_hash = credential.secret_hash
                updated = True
            if existing_user.radius_profile_id != profile_id:
                existing_user.radius_profile_id = profile_id
                updated = True
            if existing_user.is_active is not True:
                existing_user.is_active = True
                updated = True
            existing_user.last_sync_at = datetime.now(UTC)
            if updated:
                changed += 1
            continue

        db.add(
            RadiusUser(
                subscriber_id=subscription.subscriber_id,
                subscription_id=subscription.id,
                access_credential_id=credential.id,
                username=credential.username,
                secret_hash=credential.secret_hash,
                radius_profile_id=profile_id,
                is_active=True,
                last_sync_at=datetime.now(UTC),
            )
        )
        changed += 1
    return changed


def reconcile_subscription_connectivity(
    db: Session,
    subscription_id: str,
) -> dict[str, int | bool]:
    """Ensure internal RADIUS state exists for a sync-eligible subscription."""
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if not subscription or subscription.status not in RADIUS_SYNC_ELIGIBLE_STATUSES:
        return {"ok": False, "radius_clients_changed": 0, "radius_users_changed": 0}

    radius_clients_changed = 0
    if subscription.provisioning_nas_device_id:
        nas_device = db.get(NasDevice, subscription.provisioning_nas_device_id)
        if nas_device:
            radius_clients_changed = ensure_radius_clients_for_nas(db, nas_device)

    radius_users_changed = ensure_radius_users_for_subscription(db, subscription)
    db.commit()

    external_nas_synced = 0
    external_credentials_synced = 0
    credentials = (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == subscription.subscriber_id)
        .filter(AccessCredential.is_active.is_(True))
        .all()
    )
    external_configs = _active_external_sync_configs(db)
    if external_configs:
        nas_devices = [nas_device] if subscription.provisioning_nas_device_id and nas_device else []
        for config in external_configs:
            if nas_devices:
                external_nas_synced += _external_sync_nas(config, nas_devices).get(
                    "external_nas_synced", 0
                )
            if credentials:
                external_credentials_synced += _external_sync_users(
                    db, config, credentials
                ).get("external_users_synced", 0)
    else:
        for credential in credentials:
            if sync_credential_to_radius(db, credential):
                external_credentials_synced += 1

    return {
        "ok": True,
        "radius_clients_changed": radius_clients_changed,
        "radius_users_changed": radius_users_changed,
        "external_nas_synced": external_nas_synced,
        "external_credentials_synced": external_credentials_synced,
    }


_SQL_IDENT_PART_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _sanitize_table_identifier(raw: object, fallback: str) -> str:
    name = str(raw).strip() if raw is not None else fallback
    if not name:
        name = fallback
    parts = name.split(".")
    if not all(_SQL_IDENT_PART_RE.fullmatch(part) for part in parts):
        raise ValueError(f"Invalid SQL table identifier: {name!r}")
    # Identifiers are validated strictly above; quoting prevents keyword collisions.
    return ".".join(f'"{part}"' for part in parts)


def _external_db_config(db: Session, job: RadiusSyncJob) -> dict | None:
    if not job.connector_config_id:
        return None
    connector = db.get(ConnectorConfig, job.connector_config_id)
    if not connector:
        return None
    auth_config = dict(connector.auth_config or {})
    metadata = dict(connector.metadata_ or {})
    db_url = auth_config.get("db_url") or connector.base_url
    if db_url:
        db_url = _container_safe_external_db_url(resolve_secret(db_url))
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
        db_url = _container_safe_external_db_url(
            f"{driver}://{username}:{password}@{host}{port_part}/{database}"
        )
    return {
        "db_url": db_url,
        "radcheck_table": _sanitize_table_identifier(
            metadata.get("radcheck_table"), "radcheck"
        ),
        "radreply_table": _sanitize_table_identifier(
            metadata.get("radreply_table"), "radreply"
        ),
        "radusergroup_table": _sanitize_table_identifier(
            metadata.get("radusergroup_table"), "radusergroup"
        ),
        "nas_table": _sanitize_table_identifier(metadata.get("nas_table"), "nas"),
        "password_attribute": metadata.get("password_attribute", "Cleartext-Password"),
        "password_op": metadata.get("password_op", ":="),
        "use_group": bool(metadata.get("use_group", False)),
        "group_priority": int(metadata.get("group_priority", 0)),
        "default_reply_op": metadata.get("default_reply_op", ":="),
    }


def _external_sync_users(
    db: Session,
    config: dict,
    credentials: list[AccessCredential],
) -> dict[str, int]:
    from app.services.connection_type_provisioning import build_radius_reply_attributes

    radcheck = config["radcheck_table"]
    radreply = config["radreply_table"]
    radusergroup = config["radusergroup_table"]
    password_attr = config["password_attribute"]
    password_op = config["password_op"]
    use_group = config["use_group"]
    group_priority = config["group_priority"]
    default_reply_op = config["default_reply_op"]

    engine = create_engine(config["db_url"])
    created = 0
    profile_cache: dict[str, RadiusProfile | None] = {}
    with engine.begin() as conn:
        for credential in credentials:
            subscription = _radius_sync_subscription_for_subscriber(
                db, credential.subscriber_id
            )
            if not subscription:
                continue
            username = credential.username
            conn.execute(text(f"DELETE FROM {radcheck} WHERE username = :u"), {"u": username})  # noqa: S608
            conn.execute(text(f"DELETE FROM {radreply} WHERE username = :u"), {"u": username})  # noqa: S608
            if use_group:
                conn.execute(
                    text(f"DELETE FROM {radusergroup} WHERE username = :u"), {"u": username}  # noqa: S608
                )
            password_row = _external_password_row(
                credential,
                default_attribute=password_attr,
                default_op=password_op,
            )
            if password_row:
                conn.execute(
                    text(f"INSERT INTO {radcheck} (username, attribute, op, value) VALUES (:u, :attr, :op, :val)"),  # noqa: S608
                    {
                        "u": username,
                        "attr": password_row[0],
                        "op": password_row[1],
                        "val": password_row[2],
                    },
                )

            # Resolve profile from credential or subscription
            profile_id = credential.radius_profile_id or subscription.radius_profile_id
            profile: RadiusProfile | None = None
            if profile_id:
                cache_key = str(profile_id)
                if cache_key not in profile_cache:
                    profile_cache[cache_key] = db.get(RadiusProfile, profile_id)
                profile = profile_cache[cache_key]

            if use_group and profile:
                conn.execute(
                    text(f"INSERT INTO {radusergroup} (username, groupname, priority) VALUES (:u, :g, :p)"),  # noqa: S608
                    {"u": username, "g": profile.name, "p": group_priority},
                )

            # Build connection-type-aware RADIUS reply attributes
            reply_attrs = build_radius_reply_attributes(
                db, subscription, profile=profile,
            )
            seen: set[str] = set()
            for attr_dict in reply_attrs:
                attr_key = attr_dict["attribute"].lower()
                if attr_key in seen and attr_dict["op"] != "+=":
                    continue
                seen.add(attr_key)
                conn.execute(
                    text(f"INSERT INTO {radreply} (username, attribute, op, value) VALUES (:u, :attr, :op, :val)"),  # noqa: S608
                    {
                        "u": username,
                        "attr": attr_dict["attribute"],
                        "op": attr_dict.get("op") or default_reply_op,
                        "val": attr_dict["value"],
                    },
                )
            created += 1
    return {"external_users_synced": created}


def _external_sync_nas(
    config: dict,
    nas_devices: list[NasDevice],
) -> dict[str, int]:
    nas_table = config["nas_table"]
    engine = create_engine(config["db_url"])
    created = 0
    with engine.begin() as conn:
        for device in nas_devices:
            client_ip = _radius_client_ip_for_nas(device)
            if not client_ip:
                continue
            # Decrypt the stored credential, then resolve any OpenBao references
            decrypted_secret = decrypt_credential(device.shared_secret)
            secret = resolve_secret(decrypted_secret)
            if not secret:
                continue
            conn.execute(
                text(f"DELETE FROM {nas_table} WHERE nasname = :ip"),  # noqa: S608
                {"ip": client_ip},
            )
            conn.execute(
                text(f"INSERT INTO {nas_table} (nasname, shortname, type, secret, description) VALUES (:ip, :name, :type, :secret, :desc)"),  # noqa: S608
                {
                    "ip": client_ip,
                    "name": (device.name or "")[:32],
                    "type": device.vendor.value if hasattr(device.vendor, "value") else "other",
                    "secret": secret,
                    "desc": device.description,
                },
            )
            created += 1
    return {"external_nas_synced": created}


class RadiusUsers(ListResponseMixin):
    @staticmethod
    def get(db: Session, user_id: str):
        user = db.get(RadiusUser, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="Radius user not found")
        return user

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(RadiusUser)
        if account_id:
            query = query.filter(RadiusUser.subscriber_id == coerce_uuid(account_id))
        if is_active is None:
            query = query.filter(RadiusUser.is_active.is_(True))
        else:
            query = query.filter(RadiusUser.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": RadiusUser.created_at, "username": RadiusUser.username},
        )
        return apply_pagination(query, limit, offset).all()


class RadiusSyncJobs(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: RadiusSyncJobCreate):
        server = db.get(RadiusServer, payload.server_id)
        if not server:
            raise HTTPException(status_code=404, detail="Radius server not found")
        if payload.connector_config_id:
            config = db.get(ConnectorConfig, payload.connector_config_id)
            if not config:
                raise HTTPException(status_code=404, detail="Connector config not found")
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "sync_users" not in fields_set:
            default_sync_users = settings_spec.resolve_value(
                db, SettingDomain.radius, "default_sync_users"
            )
            if default_sync_users is not None:
                data["sync_users"] = bool(default_sync_users)
        if "sync_nas_clients" not in fields_set:
            default_sync_clients = settings_spec.resolve_value(
                db, SettingDomain.radius, "default_sync_nas_clients"
            )
            if default_sync_clients is not None:
                data["sync_nas_clients"] = bool(default_sync_clients)
        job = RadiusSyncJob(**data)
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    @staticmethod
    def get(db: Session, job_id: str):
        job = db.get(RadiusSyncJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Radius sync job not found")
        return job

    @staticmethod
    def list(
        db: Session,
        server_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(RadiusSyncJob)
        if server_id:
            query = query.filter(RadiusSyncJob.server_id == server_id)
        if is_active is None:
            query = query.filter(RadiusSyncJob.is_active.is_(True))
        else:
            query = query.filter(RadiusSyncJob.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": RadiusSyncJob.created_at, "name": RadiusSyncJob.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, job_id: str, payload: RadiusSyncJobUpdate):
        job = db.get(RadiusSyncJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Radius sync job not found")
        data = payload.model_dump(exclude_unset=True)
        if "server_id" in data:
            server = db.get(RadiusServer, data["server_id"])
            if not server:
                raise HTTPException(status_code=404, detail="Radius server not found")
        if "connector_config_id" in data and data["connector_config_id"]:
            config = db.get(ConnectorConfig, data["connector_config_id"])
            if not config:
                raise HTTPException(status_code=404, detail="Connector config not found")
        for key, value in data.items():
            setattr(job, key, value)
        db.commit()
        db.refresh(job)
        return job

    @staticmethod
    def delete(db: Session, job_id: str):
        job = db.get(RadiusSyncJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Radius sync job not found")
        job.is_active = False
        db.commit()

    @staticmethod
    def run(db: Session, job_id: str):
        job = db.get(RadiusSyncJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Radius sync job not found")
        if not job.is_active:
            raise HTTPException(status_code=400, detail="Radius sync job is inactive")
        default_status = settings_spec.resolve_value(
            db, SettingDomain.radius, "default_sync_status"
        )
        status_value = (
            validate_enum(default_status, RadiusSyncStatus, "status")
            if default_status
            else RadiusSyncStatus.running
        )
        run = RadiusSyncRun(job_id=job.id, status=status_value)
        db.add(run)
        db.commit()
        db.refresh(run)

        started_at = datetime.now(UTC)
        users_created = users_updated = clients_created = clients_updated = 0
        status = RadiusSyncStatus.success
        details: dict[str, object] = {}
        try:
            external_config = _external_db_config(db, job)
            if job.sync_nas_clients:
                nas_devices = (
                    db.query(NasDevice)
                    .filter(NasDevice.is_active.is_(True))
                    .all()
                )
                for device in nas_devices:
                    client_ip = _radius_client_ip_for_nas(device)
                    if not client_ip:
                        continue
                    # Decrypt the stored credential, then resolve any OpenBao references
                    decrypted_secret = decrypt_credential(device.shared_secret)
                    raw_secret = resolve_secret(decrypted_secret)
                    if not raw_secret:
                        continue
                    existing_client = (
                        db.query(RadiusClient)
                        .filter(RadiusClient.server_id == job.server_id)
                        .filter(RadiusClient.client_ip == client_ip)
                        .first()
                    )
                    secret_hash = _hash_secret(raw_secret)
                    if existing_client:
                        existing_client.nas_device_id = device.id
                        existing_client.client_ip = client_ip
                        existing_client.shared_secret_hash = secret_hash
                        existing_client.description = device.name
                        existing_client.is_active = True
                        clients_updated += 1
                    else:
                        client = RadiusClient(
                            server_id=job.server_id,
                            nas_device_id=device.id,
                            client_ip=client_ip,
                            shared_secret_hash=secret_hash,
                            description=device.name,
                            is_active=True,
                        )
                        db.add(client)
                        clients_created += 1
                db.commit()
                details["nas_devices_synced"] = len(nas_devices)
                if external_config:
                    details.update(_external_sync_nas(external_config, nas_devices))

            if job.sync_users:
                credentials = (
                    db.query(AccessCredential)
                    .filter(AccessCredential.is_active.is_(True))
                    .all()
                )
                for credential in credentials:
                    subscription = _radius_sync_subscription_for_subscriber(
                        db, credential.subscriber_id
                    )
                    if not subscription:
                        continue
                    existing_user = (
                        db.query(RadiusUser)
                        .filter(RadiusUser.access_credential_id == credential.id)
                        .first()
                    )
                    if existing_user:
                        existing_user.subscription_id = subscription.id
                        existing_user.subscriber_id = subscription.subscriber_id
                        existing_user.username = credential.username
                        existing_user.secret_hash = credential.secret_hash
                        existing_user.radius_profile_id = credential.radius_profile_id
                        existing_user.is_active = True
                        existing_user.last_sync_at = datetime.now(UTC)
                        users_updated += 1
                    else:
                        user = RadiusUser(
                            subscriber_id=subscription.subscriber_id,
                            subscription_id=subscription.id,
                            access_credential_id=credential.id,
                            username=credential.username,
                            secret_hash=credential.secret_hash,
                            radius_profile_id=credential.radius_profile_id,
                            is_active=True,
                            last_sync_at=datetime.now(UTC),
                        )
                        db.add(user)
                        users_created += 1
                db.commit()
                details["credentials_scanned"] = len(credentials)
                if external_config:
                    details.update(_external_sync_users(db, external_config, credentials))
        except Exception as exc:
            db.rollback()
            status = RadiusSyncStatus.failed
            details["error"] = str(exc)
            raise
        finally:
            finished_at = datetime.now(UTC)
            run.status = status
            run.finished_at = finished_at
            run.users_created = users_created
            run.users_updated = users_updated
            run.clients_created = clients_created
            run.clients_updated = clients_updated
            run.details = details
            job.last_run_at = finished_at
            db.commit()
            observe_job("radius_sync", status.value, (finished_at - started_at).total_seconds())
            db.refresh(run)
        return run


class RadiusSyncRuns(ListResponseMixin):
    @staticmethod
    def get(db: Session, run_id: str):
        run = db.get(RadiusSyncRun, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Radius sync run not found")
        return run

    @staticmethod
    def list(
        db: Session,
        job_id: str | None,
        status: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(RadiusSyncRun)
        if job_id:
            query = query.filter(RadiusSyncRun.job_id == job_id)
        if status:
            try:
                status_value = RadiusSyncStatus(status)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid status") from exc
            query = query.filter(RadiusSyncRun.status == status_value)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"started_at": RadiusSyncRun.started_at, "status": RadiusSyncRun.status},
        )
        return apply_pagination(query, limit, offset).all()


def sync_credential_to_radius(db: Session, credential: AccessCredential) -> bool:
    """Immediately sync a single credential to all active RADIUS sync jobs.

    This is called when a credential is created/updated or when a subscription
    is activated, ensuring the user can authenticate immediately without
    waiting for the periodic sync.

    Args:
        db: Database session
        credential: The access credential to sync

    Returns:
        True if synced to at least one external RADIUS database
    """
    if not credential.is_active:
        return False

    # Check if credential has a sync-eligible subscription
    subscription = _radius_sync_subscription_for_subscriber(
        db, credential.subscriber_id
    )
    if not subscription:
        return False

    # Find all active sync jobs with external connectors
    sync_jobs = (
        db.query(RadiusSyncJob)
        .filter(RadiusSyncJob.is_active.is_(True))
        .filter(RadiusSyncJob.sync_users.is_(True))
        .filter(RadiusSyncJob.connector_config_id.isnot(None))
        .all()
    )

    synced = False
    for job in sync_jobs:
        config = _external_db_config(db, job)
        if not config:
            continue
        try:
            _external_sync_users(db, config, [credential])
            synced = True
        except Exception:
            # Log but don't fail - the periodic sync will catch it
            logger.warning(
                "Failed to sync credential %s to RADIUS job %s",
                credential.username,
                job.id,
            )

    return synced


def sync_account_credentials_to_radius(db: Session, account_id) -> int:
    """Sync all active credentials for an account to RADIUS.

    Called when a subscription is activated to ensure all the account's
    credentials are immediately available for authentication.

    Args:
        db: Database session
        account_id: The subscriber account ID

    Returns:
        Number of credentials synced
    """
    account_uuid = coerce_uuid(account_id)
    credentials = (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == account_uuid)
        .filter(AccessCredential.is_active.is_(True))
        .all()
    )

    count = 0
    for credential in credentials:
        if sync_credential_to_radius(db, credential):
            count += 1

    return count


def remove_external_radius_credentials(db: Session, account_id) -> int:
    """Remove all RADIUS credentials for an account from external RADIUS databases.

    Called on subscription suspension/cancellation to prevent the subscriber
    from authenticating until reactivated.

    Args:
        db: Database session
        account_id: The subscriber account ID

    Returns:
        Number of credentials removed from external RADIUS
    """
    account_uuid = coerce_uuid(account_id)
    credentials = (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == account_uuid)
        .all()
    )
    if not credentials:
        return 0

    external_configs = _active_external_sync_configs(db)
    if not external_configs:
        return 0

    removed = 0
    for config in external_configs:
        radcheck = config["radcheck_table"]
        radreply = config["radreply_table"]
        radusergroup = config.get("radusergroup_table", "radusergroup")
        try:
            engine = create_engine(config["db_url"])
            with engine.begin() as conn:
                for credential in credentials:
                    conn.execute(
                        text(f"DELETE FROM {radcheck} WHERE username = :u"),  # noqa: S608
                        {"u": credential.username},
                    )
                    conn.execute(
                        text(f"DELETE FROM {radreply} WHERE username = :u"),  # noqa: S608
                        {"u": credential.username},
                    )
                    conn.execute(
                        text(f"DELETE FROM {radusergroup} WHERE username = :u"),  # noqa: S608
                        {"u": credential.username},
                    )
                    removed += 1
            logger.info(
                "Removed %d credentials from external RADIUS for account %s",
                len(credentials),
                account_id,
            )
        except Exception:
            logger.warning(
                "Failed to remove credentials from external RADIUS for account %s",
                account_id,
                exc_info=True,
            )

    return removed


radius_servers = RadiusServers()
radius_clients = RadiusClients()
radius_users = RadiusUsers()
radius_sync_jobs = RadiusSyncJobs()
radius_sync_runs = RadiusSyncRuns()
