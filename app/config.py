import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "development")).lower()
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@localhost:5434/dotmac_sub",
    )
    # Pool is per-process; the engine is recreated in every uvicorn worker and
    # every Celery prefork process. With ~8 such processes (4 uvicorn + Celery
    # workers + beat), 30+30 per process could demand ~480 connections and
    # exhaust Postgres' max_connections (default 100) before pool_timeout ever
    # engages. 20+10 keeps the fleet's ceiling well under a 300-conn server and
    # makes the app queue on pool_timeout (graceful) rather than getting hard
    # "too many clients" rejections. Override per-process via env if needed.
    db_pool_size: int = int(os.getenv("DB_POOL_SIZE", "20"))
    db_max_overflow: int = int(os.getenv("DB_MAX_OVERFLOW", "10"))
    # Cap the AnyIO threadpool that runs sync request handlers so a uvicorn
    # worker never schedules more concurrent DB-touching threads than its pool
    # can serve (default AnyIO limit is 40 > pool of 30). Applied in the API
    # lifespan only; Celery sets its own concurrency.
    web_threadpool_limit: int = int(os.getenv("WEB_THREADPOOL_LIMIT", "30"))
    db_pool_timeout: int = int(os.getenv("DB_POOL_TIMEOUT", "30"))
    db_pool_recycle: int = int(os.getenv("DB_POOL_RECYCLE", "1800"))
    db_statement_timeout_ms: int = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "120000"))
    db_lock_timeout_ms: int = int(os.getenv("DB_LOCK_TIMEOUT_MS", "10000"))
    db_idle_in_transaction_session_timeout_ms: int = int(
        os.getenv("DB_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS", "120000")
    )

    # Avatar settings
    avatar_upload_dir: str = os.getenv("AVATAR_UPLOAD_DIR", "static/avatars")
    avatar_max_size_bytes: int = int(
        os.getenv("AVATAR_MAX_SIZE_BYTES", str(2 * 1024 * 1024))
    )  # 2MB
    avatar_allowed_types: str = os.getenv(
        "AVATAR_ALLOWED_TYPES", "image/jpeg,image/png,image/gif,image/webp"
    )
    avatar_url_prefix: str = os.getenv("AVATAR_URL_PREFIX", "/static/avatars")

    # Splynx is decommissioned (2026-06-16; local ledger is the sole source of
    # truth). The incremental-sync machinery and its sync-state tables are gone
    # (migration 169). The remote-MySQL connection settings that fed that sync
    # have been removed — nothing read them. Remove SPLYNX_MYSQL_* from .env too;
    # rotate any value that was a live credential. Historical Splynx data
    # (splynx_billing_transactions, id mappings, archives, splynx_* id columns)
    # is retained READ-ONLY for audit/reconciliation only.

    # Cookie security
    secure_cookies: bool = os.getenv("SECURE_COOKIES", "true").lower() in (
        "true",
        "1",
        "yes",
    )

    # DEM settings
    dem_data_dir: str = os.getenv("DEM_DATA_DIR", "data/dem/srtm")

    # Meta Graph API settings
    meta_graph_api_version: str = os.getenv("META_GRAPH_API_VERSION", "v21.0")
    meta_graph_base_url: str = os.getenv(
        "META_GRAPH_BASE_URL",
        f"https://graph.facebook.com/{os.getenv('META_GRAPH_API_VERSION', 'v19.0')}",
    )

    # CRM integration (DotMac Omni CRM)
    crm_base_url: str = os.getenv("CRM_BASE_URL", "")
    crm_username: str = os.getenv("CRM_USERNAME", "")
    crm_password: str = os.getenv("CRM_PASSWORD", "")
    # Shared secret for inbound CRM webhook deliveries (HMAC-SHA256).
    crm_webhook_secret: str = os.getenv("CRM_WEBHOOK_SECRET", "")
    # Dedicated bearer token for CRM server-to-server pull/write-back API.
    # This is intentionally separate from CRM_WEBHOOK_SECRET, which protects
    # inbound HMAC-signed webhook deliveries.
    selfcare_api_token: str = os.getenv("SELFCARE_API_TOKEN", "")

    # Mono lookup API
    mono_secret_key: str = os.getenv("MONO_SECRET_KEY", "")
    mono_base_url: str = os.getenv("MONO_BASE_URL", "https://api.withmono.com")
    mono_timeout_seconds: float = float(os.getenv("MONO_TIMEOUT_SECONDS", "15"))

    # S3-compatible object storage
    s3_endpoint_url: str = os.getenv("S3_ENDPOINT_URL", "http://minio:9000")
    s3_access_key: str = os.getenv("S3_ACCESS_KEY", "")
    s3_secret_key: str = os.getenv("S3_SECRET_KEY", "")
    s3_bucket_name: str = os.getenv("S3_BUCKET_NAME", "dotmac-private")
    s3_region: str = os.getenv("S3_REGION", "us-east-1")
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    REDIS_URL: str = redis_url

    # Router Management
    router_sync_interval_hours: int = int(os.getenv("ROUTER_SYNC_INTERVAL_HOURS", "6"))
    router_interface_sync_interval_min: int = int(
        os.getenv("ROUTER_IFACE_SYNC_INTERVAL_MIN", "15")
    )
    router_snapshot_schedule: str = os.getenv("ROUTER_SNAPSHOT_SCHEDULE", "0 2 * * *")
    router_tunnel_cleanup_interval_min: int = int(
        os.getenv("ROUTER_TUNNEL_CLEANUP_MIN", "5")
    )

    # TR-069 settings
    tr069_periodic_inform_interval: int = int(
        os.getenv("TR069_PERIODIC_INFORM_INTERVAL", "300")
    )  # seconds, default 5 minutes
    tr069_auth_shared_secret: str = os.getenv("TR069_AUTH_SHARED_SECRET", "")
    acs_routable_management_cidrs: str = os.getenv(
        "ACS_ROUTABLE_MANAGEMENT_CIDRS",
        "172.16.0.0/16",
    )

    # Security: Enforce credential encryption in production
    # Set to "true" to require CREDENTIAL_ENCRYPTION_KEY to be configured
    enforce_credential_encryption: bool = os.getenv(
        "ENFORCE_CREDENTIAL_ENCRYPTION",
        "true" if app_env in {"prod", "production"} else "false",
    ).lower() in ("true", "1", "yes")

    # Layer 3 (identity/email decoupling): allow reseller portal logins to
    # authenticate as a first-class ResellerUser principal instead of a fake
    # Subscriber. Default OFF — the dual-read auth code is inert until flipped,
    # and a backfill must repoint reseller credentials before cutover.
    reseller_user_principal_enabled: bool = os.getenv(
        "RESELLER_USER_PRINCIPAL_ENABLED", "false"
    ).lower() in ("true", "1", "yes")


settings = Settings()
