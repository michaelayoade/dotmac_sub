import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@localhost:5434/dotmac_sm",
    )
    db_pool_size: int = int(os.getenv("DB_POOL_SIZE", "15"))
    db_max_overflow: int = int(os.getenv("DB_MAX_OVERFLOW", "20"))
    db_pool_timeout: int = int(os.getenv("DB_POOL_TIMEOUT", "30"))
    db_pool_recycle: int = int(os.getenv("DB_POOL_RECYCLE", "1800"))

    # Avatar settings
    avatar_upload_dir: str = os.getenv("AVATAR_UPLOAD_DIR", "static/avatars")
    avatar_max_size_bytes: int = int(
        os.getenv("AVATAR_MAX_SIZE_BYTES", str(2 * 1024 * 1024))
    )  # 2MB
    avatar_allowed_types: str = os.getenv(
        "AVATAR_ALLOWED_TYPES", "image/jpeg,image/png,image/gif,image/webp"
    )
    avatar_url_prefix: str = os.getenv("AVATAR_URL_PREFIX", "/static/avatars")

    # Splynx MySQL sync settings (for incremental sync from remote Splynx DB)
    mysql_host: str = os.getenv("SPLYNX_MYSQL_HOST", "127.0.0.1")
    mysql_port: int = int(os.getenv("SPLYNX_MYSQL_PORT", "3306"))
    mysql_user: str = os.getenv("SPLYNX_MYSQL_USER", "splynx")
    mysql_password: str = os.getenv("SPLYNX_MYSQL_PASSWORD", "")
    mysql_database: str = os.getenv("SPLYNX_MYSQL_DATABASE", "splynx")

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

    # S3-compatible object storage
    s3_endpoint_url: str = os.getenv("S3_ENDPOINT_URL", "http://minio:9000")
    s3_access_key: str = os.getenv("S3_ACCESS_KEY", "")
    s3_secret_key: str = os.getenv("S3_SECRET_KEY", "")
    s3_bucket_name: str = os.getenv("S3_BUCKET_NAME", "dotmac-private")
    s3_region: str = os.getenv("S3_REGION", "us-east-1")

    # Router Management
    router_sync_interval_hours: int = int(os.getenv("ROUTER_SYNC_INTERVAL_HOURS", "6"))
    router_interface_sync_interval_min: int = int(os.getenv("ROUTER_IFACE_SYNC_INTERVAL_MIN", "15"))
    router_snapshot_schedule: str = os.getenv("ROUTER_SNAPSHOT_SCHEDULE", "0 2 * * *")
    router_tunnel_cleanup_interval_min: int = int(os.getenv("ROUTER_TUNNEL_CLEANUP_MIN", "5"))

    # Security: Enforce credential encryption in production
    # Set to "true" to require CREDENTIAL_ENCRYPTION_KEY to be configured
    enforce_credential_encryption: bool = os.getenv(
        "ENFORCE_CREDENTIAL_ENCRYPTION", "false"
    ).lower() in ("true", "1", "yes")


settings = Settings()
