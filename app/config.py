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
    avatar_max_size_bytes: int = int(os.getenv("AVATAR_MAX_SIZE_BYTES", str(2 * 1024 * 1024)))  # 2MB
    avatar_allowed_types: str = os.getenv("AVATAR_ALLOWED_TYPES", "image/jpeg,image/png,image/gif,image/webp")
    avatar_url_prefix: str = os.getenv("AVATAR_URL_PREFIX", "/static/avatars")

    # Splynx MySQL sync settings (for incremental sync from remote Splynx DB)
    mysql_host: str = os.getenv("SPLYNX_MYSQL_HOST", "127.0.0.1")
    mysql_port: int = int(os.getenv("SPLYNX_MYSQL_PORT", "3306"))
    mysql_user: str = os.getenv("SPLYNX_MYSQL_USER", "splynx")
    mysql_password: str = os.getenv("SPLYNX_MYSQL_PASSWORD", "")
    mysql_database: str = os.getenv("SPLYNX_MYSQL_DATABASE", "splynx")

    # Cookie security
    secure_cookies: bool = os.getenv("SECURE_COOKIES", "true").lower() in ("true", "1", "yes")

    # DEM settings
    dem_data_dir: str = os.getenv("DEM_DATA_DIR", "data/dem/srtm")

    # Meta Graph API settings
    meta_graph_api_version: str = os.getenv("META_GRAPH_API_VERSION", "v21.0")
    meta_graph_base_url: str = os.getenv(
        "META_GRAPH_BASE_URL",
        f"https://graph.facebook.com/{os.getenv('META_GRAPH_API_VERSION', 'v19.0')}",
    )


settings = Settings()
