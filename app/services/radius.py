import hashlib
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.metrics import observe_job
from app.services.common import apply_ordering, apply_pagination, coerce_uuid, validate_enum
from app.models.catalog import (
    AccessCredential,
    NasDevice,
    RadiusAttribute,
    RadiusProfile,
    Subscription,
    SubscriptionStatus,
)
from app.services.response import ListResponseMixin
from app.models.connector import ConnectorConfig
from app.models.radius import (
    RadiusClient,
    RadiusServer,
    RadiusSyncJob,
    RadiusSyncRun,
    RadiusSyncStatus,
    RadiusUser,
)
from app.models.domain_settings import SettingDomain
from app.services.credential_crypto import decrypt_credential
from app.services.secrets import resolve_secret
from app.schemas.radius import (
    RadiusClientCreate,
    RadiusClientUpdate,
    RadiusSyncJobCreate,
    RadiusSyncJobUpdate,
    RadiusServerCreate,
    RadiusServerUpdate,
)
from app.services import settings_spec


class RadiusServers(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: RadiusServerCreate):
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "auth_port" not in fields_set:
            default_auth_port = settings_spec.resolve_value(
                db, SettingDomain.radius, "default_auth_port"
            )
            if default_auth_port:
                data["auth_port"] = int(default_auth_port)
        if "acct_port" not in fields_set:
            default_acct_port = settings_spec.resolve_value(
                db, SettingDomain.radius, "default_acct_port"
            )
            if default_acct_port:
                data["acct_port"] = int(default_acct_port)
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
        db_url = resolve_secret(db_url)
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
        db_url = f"{driver}://{username}:{password}@{host}{port_part}/{database}"
    return {
        "db_url": db_url,
        "radcheck_table": metadata.get("radcheck_table", "radcheck"),
        "radreply_table": metadata.get("radreply_table", "radreply"),
        "radusergroup_table": metadata.get("radusergroup_table", "radusergroup"),
        "nas_table": metadata.get("nas_table", "nas"),
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
    profile_cache: dict[str, tuple[RadiusProfile | None, list[RadiusAttribute]]] = {}
    with engine.begin() as conn:
        for credential in credentials:
            subscription = (
                db.query(Subscription)
                .filter(Subscription.subscriber_id == credential.subscriber_id)
                .filter(Subscription.status == SubscriptionStatus.active)
                .order_by(
                    Subscription.start_at.desc().nullslast(),
                    Subscription.created_at.desc(),
                )
                .first()
            )
            if not subscription:
                continue
            username = credential.username
            conn.execute(text(f"DELETE FROM {radcheck} WHERE username = :u"), {"u": username})
            conn.execute(text(f"DELETE FROM {radreply} WHERE username = :u"), {"u": username})
            if use_group:
                conn.execute(
                    text(f"DELETE FROM {radusergroup} WHERE username = :u"), {"u": username}
                )
            if credential.secret_hash:
                conn.execute(
                    text(
                        f"INSERT INTO {radcheck} (username, attribute, op, value) "
                        "VALUES (:u, :attr, :op, :val)"
                    ),
                    {
                        "u": username,
                        "attr": password_attr,
                        "op": password_op,
                        "val": credential.secret_hash,
                    },
                )
            if credential.radius_profile_id:
                cache_key = str(credential.radius_profile_id)
                if cache_key not in profile_cache:
                    profile = db.get(RadiusProfile, credential.radius_profile_id)
                    attributes = []
                    if profile:
                        attributes = (
                            db.query(RadiusAttribute)
                            .filter(RadiusAttribute.profile_id == profile.id)
                            .all()
                        )
                    profile_cache[cache_key] = (profile, attributes)
                profile, attributes = profile_cache[cache_key]
                if use_group and profile:
                    conn.execute(
                        text(
                            f"INSERT INTO {radusergroup} (username, groupname, priority) "
                            "VALUES (:u, :g, :p)"
                        ),
                        {"u": username, "g": profile.name, "p": group_priority},
                    )
                if profile and profile.mikrotik_address_list:
                    has_address_list = any(
                        attr.attribute.lower() == "mikrotik-address-list"
                        for attr in attributes
                    )
                    if not has_address_list:
                        conn.execute(
                            text(
                                f"INSERT INTO {radreply} (username, attribute, op, value) "
                                "VALUES (:u, :attr, :op, :val)"
                            ),
                            {
                                "u": username,
                                "attr": "Mikrotik-Address-List",
                                "op": default_reply_op,
                                "val": profile.mikrotik_address_list,
                            },
                        )
                for attr in attributes:
                    conn.execute(
                        text(
                            f"INSERT INTO {radreply} (username, attribute, op, value) "
                            "VALUES (:u, :attr, :op, :val)"
                        ),
                        {
                            "u": username,
                            "attr": attr.attribute,
                            "op": attr.operator or default_reply_op,
                            "val": attr.value,
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
            if not device.ip_address:
                continue
            # Decrypt the stored credential, then resolve any OpenBao references
            decrypted_secret = decrypt_credential(device.shared_secret)
            secret = resolve_secret(decrypted_secret)
            if not secret:
                continue
            conn.execute(
                text(f"DELETE FROM {nas_table} WHERE nasname = :ip"),
                {"ip": device.ip_address},
            )
            conn.execute(
                text(
                    f"INSERT INTO {nas_table} (nasname, shortname, type, secret, description) "
                    "VALUES (:ip, :name, :type, :secret, :desc)"
                ),
                {
                    "ip": device.ip_address,
                    "name": device.name,
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
            query = query.filter(RadiusUser.account_id == account_id)
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

        started_at = datetime.now(timezone.utc)
        users_created = users_updated = clients_created = clients_updated = 0
        status = RadiusSyncStatus.success
        details: dict[str, int] = {}
        try:
            external_config = _external_db_config(db, job)
            if job.sync_nas_clients:
                nas_devices = (
                    db.query(NasDevice)
                    .filter(NasDevice.is_active.is_(True))
                    .filter(NasDevice.ip_address.isnot(None))
                    .all()
                )
                for device in nas_devices:
                    # Decrypt the stored credential, then resolve any OpenBao references
                    decrypted_secret = decrypt_credential(device.shared_secret)
                    raw_secret = resolve_secret(decrypted_secret)
                    if not raw_secret:
                        continue
                    existing = (
                        db.query(RadiusClient)
                        .filter(RadiusClient.server_id == job.server_id)
                        .filter(RadiusClient.client_ip == device.ip_address)
                        .first()
                    )
                    secret_hash = _hash_secret(raw_secret)
                    if existing:
                        existing.nas_device_id = device.id
                        existing.shared_secret_hash = secret_hash
                        existing.description = device.name
                        existing.is_active = True
                        clients_updated += 1
                    else:
                        client = RadiusClient(
                            server_id=job.server_id,
                            nas_device_id=device.id,
                            client_ip=device.ip_address,
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
                    subscription = (
                        db.query(Subscription)
                        .filter(Subscription.subscriber_id == credential.subscriber_id)
                        .filter(Subscription.status == SubscriptionStatus.active)
                        .order_by(Subscription.start_at.desc().nullslast(), Subscription.created_at.desc())
                        .first()
                    )
                    if not subscription:
                        continue
                    existing = (
                        db.query(RadiusUser)
                        .filter(RadiusUser.access_credential_id == credential.id)
                        .first()
                    )
                    if existing:
                        existing.subscription_id = subscription.id
                        existing.account_id = subscription.subscriber_id
                        existing.username = credential.username
                        existing.secret_hash = credential.secret_hash
                        existing.radius_profile_id = credential.radius_profile_id
                        existing.is_active = True
                        existing.last_sync_at = datetime.now(timezone.utc)
                        users_updated += 1
                    else:
                        user = RadiusUser(
                            account_id=subscription.subscriber_id,
                            subscription_id=subscription.id,
                            access_credential_id=credential.id,
                            username=credential.username,
                            secret_hash=credential.secret_hash,
                            radius_profile_id=credential.radius_profile_id,
                            is_active=True,
                            last_sync_at=datetime.now(timezone.utc),
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
            finished_at = datetime.now(timezone.utc)
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

    # Check if credential has an active subscription
    subscription = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == credential.subscriber_id)
        .filter(Subscription.status == SubscriptionStatus.active)
        .order_by(Subscription.start_at.desc().nullslast(), Subscription.created_at.desc())
        .first()
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
            import logging
            logging.getLogger(__name__).warning(
                f"Failed to sync credential {credential.username} to RADIUS job {job.id}"
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
    from app.services.common import coerce_uuid

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


radius_servers = RadiusServers()
radius_clients = RadiusClients()
radius_users = RadiusUsers()
radius_sync_jobs = RadiusSyncJobs()
radius_sync_runs = RadiusSyncRuns()
