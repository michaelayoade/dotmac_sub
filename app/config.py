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

    # Live chat (bridges to the CRM chat_widget channel). Default OFF: the
    # broker endpoints return 503 until a deploy flips this on deliberately.
    chat_live_enabled: bool = os.getenv("CHAT_LIVE_ENABLED", "false").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    # ChatWidgetConfig id in the CRM that customer + reseller sessions attach to
    # (general support pool; same config for both surfaces).
    crm_chat_config_id: str = os.getenv("CRM_CHAT_CONFIG_ID", "")
    # The inbound chat push webhook shares crm_webhook_secret with the other CRM
    # webhooks (the CRM signs it with the same selfcare secret), so no separate
    # chat secret is needed.
    # Visitor WebSocket URL handed to clients. Derived from crm_base_url
    # (https→wss) + /ws/widget when left blank.
    crm_chat_ws_url: str = os.getenv("CRM_CHAT_WS_URL", "")

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
    # Config snapshots are captured over SSH `/export`: RouterOS 7.x REST cannot
    # return config text (inline /export is empty; exported files aren't readable
    # back over REST). Uses the dedicated dotmac-ops SSH key, not the API/REST
    # identity. Set ROUTER_CONFIG_EXPORT_VIA_SSH=false to fall back to REST.
    router_config_export_via_ssh: bool = os.getenv(
        "ROUTER_CONFIG_EXPORT_VIA_SSH", "true"
    ).lower() in ("true", "1", "yes")
    router_config_ssh_username: str = os.getenv(
        "ROUTER_CONFIG_SSH_USERNAME", "dotmac-ops"
    )
    router_config_ssh_port: int = int(os.getenv("ROUTER_CONFIG_SSH_PORT", "120"))
    router_config_ssh_key_path: str = os.getenv(
        "ROUTER_CONFIG_SSH_KEY_PATH", "/etc/dotmac/dotmac-ops.key"
    )
    # Optional password auth for a least-privilege (ssh,read) snapshot user, as
    # an alternative/fallback to the key. Simplifies per-router onboarding (one
    # `/user add password=...` line — no public-key file import). Key is
    # preferred: the password is only used when no key is configured, or when a
    # router rejects the key (e.g. a not-yet-keyed new router). May be a plain
    # value or an OpenBao/secret ref (bao://…) resolved at use.
    router_config_ssh_password: str = os.getenv("ROUTER_CONFIG_SSH_PASSWORD", "")
    # Host-key pinning (TOFU): known_hosts persists first-seen router keys so a
    # CHANGED key is rejected (MITM guard). Strict mode also rejects unknown
    # hosts (requires a pre-populated known_hosts file).
    router_config_ssh_known_hosts_path: str = os.getenv(
        "ROUTER_CONFIG_SSH_KNOWN_HOSTS_PATH", "/etc/dotmac/router_known_hosts"
    )
    router_config_ssh_strict_host_key: bool = os.getenv(
        "ROUTER_CONFIG_SSH_STRICT_HOST_KEY", "false"
    ).lower() in ("true", "1", "yes")

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

    # Infrastructure SLA: when ON, the live-status warmer records device
    # availability transitions as uptime Alert intervals (down->open,
    # recovered->resolve). This is what populates the SLA/uptime report —
    # without it, no uptime alerts exist in prod and every uptime % reads 100%
    # (see INFRASTRUCTURE_SLA_PERFORMANCE.md Phase 0 / R1). Default OFF: the
    # warmer is a hot deployed task, so the bridge is inert until a deploy flips
    # this on deliberately. Additive-only — never changes live_status behaviour.
    sla_availability_log_enabled: bool = os.getenv(
        "SLA_AVAILABILITY_LOG_ENABLED", "false"
    ).lower() in ("true", "1", "yes")

    # Global infrastructure-SLA uptime target (%). Network elements have no
    # per-element SLA profile (SlaProfile binds to catalog offers, not devices),
    # so the performance dashboard's PASS/BREACH badge compares every element's
    # measured uptime % against this single target. Per-tier overrides are a
    # later refinement.
    infra_sla_target_percent: float = float(
        os.getenv("INFRA_SLA_TARGET_PERCENT", "99.5")
    )


settings = Settings()
