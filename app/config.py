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

    # DEM settings
    dem_data_dir: str = os.getenv("DEM_DATA_DIR", "data/dem/srtm")

    # Ticket attachment settings
    ticket_attachment_upload_dir: str = os.getenv(
        "TICKET_ATTACHMENT_UPLOAD_DIR", "static/uploads/tickets"
    )
    ticket_attachment_url_prefix: str = os.getenv(
        "TICKET_ATTACHMENT_URL_PREFIX", "/static/uploads/tickets"
    )
    ticket_attachment_max_size_bytes: int = int(
        os.getenv("TICKET_ATTACHMENT_MAX_SIZE_BYTES", str(5 * 1024 * 1024))
    )
    ticket_attachment_allowed_types: str = os.getenv(
        "TICKET_ATTACHMENT_ALLOWED_TYPES",
        "image/jpeg,image/png,image/gif,image/webp,application/pdf",
    )

    # Meta Graph API settings
    meta_graph_api_version: str = os.getenv("META_GRAPH_API_VERSION", "v21.0")
    meta_graph_base_url: str = os.getenv(
        "META_GRAPH_BASE_URL",
        f"https://graph.facebook.com/{os.getenv('META_GRAPH_API_VERSION', 'v19.0')}",
    )


settings = Settings()
