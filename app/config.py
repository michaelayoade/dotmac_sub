import os
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

load_dotenv()


class Settings(BaseModel):
    database_url: str = Field(
        default=os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg://postgres:postgres@localhost:5434/dotmac_sm",
        )
    )
    db_pool_size: int = Field(default=int(os.getenv("DB_POOL_SIZE", "15")))
    db_max_overflow: int = Field(default=int(os.getenv("DB_MAX_OVERFLOW", "20")))
    db_pool_timeout: int = Field(default=int(os.getenv("DB_POOL_TIMEOUT", "30")))
    db_pool_recycle: int = Field(default=int(os.getenv("DB_POOL_RECYCLE", "1800")))

    # Avatar settings
    avatar_upload_dir: str = Field(default=os.getenv("AVATAR_UPLOAD_DIR", "static/avatars"))
    avatar_max_size_bytes: int = Field(
        default=int(os.getenv("AVATAR_MAX_SIZE_BYTES", str(2 * 1024 * 1024)))
    )  # 2MB
    avatar_allowed_types: str = Field(
        default=os.getenv("AVATAR_ALLOWED_TYPES", "image/jpeg,image/png,image/gif,image/webp")
    )
    avatar_url_prefix: str = Field(default=os.getenv("AVATAR_URL_PREFIX", "/static/avatars"))

    # Splynx MySQL sync settings (for incremental sync from remote Splynx DB)
    mysql_host: str = Field(default=os.getenv("SPLYNX_MYSQL_HOST", "127.0.0.1"))
    mysql_port: int = Field(default=int(os.getenv("SPLYNX_MYSQL_PORT", "3306")))
    mysql_user: str = Field(default=os.getenv("SPLYNX_MYSQL_USER", "splynx"))
    mysql_password: str = Field(default=os.getenv("SPLYNX_MYSQL_PASSWORD", ""))
    mysql_database: str = Field(default=os.getenv("SPLYNX_MYSQL_DATABASE", "splynx"))

    # Cookie security
    secure_cookies: bool = Field(
        default=os.getenv("SECURE_COOKIES", "true").lower() in ("true", "1", "yes")
    )

    # DEM settings
    dem_data_dir: str = Field(default=os.getenv("DEM_DATA_DIR", "data/dem/srtm"))

    # Meta Graph API settings
    meta_graph_api_version: str = Field(default=os.getenv("META_GRAPH_API_VERSION", "v21.0"))
    meta_graph_base_url: str = Field(
        default=os.getenv(
            "META_GRAPH_BASE_URL",
            f"https://graph.facebook.com/{os.getenv('META_GRAPH_API_VERSION', 'v19.0')}",
        )
    )

    # S3-compatible object storage
    s3_endpoint_url: str = Field(default=os.getenv("S3_ENDPOINT_URL", "http://minio:9000"))
    s3_access_key: Optional[str] = Field(default=os.getenv("S3_ACCESS_KEY"))
    s3_secret_key: Optional[str] = Field(default=os.getenv("S3_SECRET_KEY"))
    s3_bucket_name: str = Field(default=os.getenv("S3_BUCKET_NAME", "dotmac-private"))
    s3_region: str = Field(default=os.getenv("S3_REGION", "us-east-1"))

    @field_validator('s3_access_key', 's3_secret_key', mode='after')
    @classmethod
    def validate_s3_credentials(cls, v: Optional[str], info) -> Optional[str]:
        # This validator runs for each field individually
        # We need to check both fields when validating the entire model
        return v

    def validate_s3_config(self) -> None:
        """Validate S3 configuration when actually needed."""
        if self.s3_access_key is None or self.s3_secret_key is None:
            raise ValueError('S3_ACCESS_KEY and S3_SECRET_KEY must be configured')

    class Config:
        frozen = True  # Makes the model immutable like the original dataclass


settings = Settings()
