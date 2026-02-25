"""S3-compatible private object storage service."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from app.config import settings

logger = logging.getLogger(__name__)


class ObjectStorageError(Exception):
    """Generic object storage failure."""


class ObjectNotFoundError(ObjectStorageError):
    """Raised when object is missing."""


@dataclass
class StreamResult:
    """Streaming metadata for download responses."""

    chunks: Iterator[bytes]
    content_type: str | None
    content_length: int | None


class StorageService(Protocol):
    """Storage provider interface."""

    def upload(self, key: str, data: bytes, content_type: str | None) -> None: ...
    def download(self, key: str) -> bytes: ...
    def stream(self, key: str) -> StreamResult: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


class S3StorageService:
    """S3/MinIO/R2-backed storage provider."""

    def __init__(
        self,
        bucket_name: str,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region: str,
        client: Any | None = None,
    ) -> None:
        self.bucket_name = bucket_name
        self.region = region
        if client is not None:
            self.client = client
            return
        try:
            import boto3
        except ImportError as exc:
            raise ObjectStorageError("boto3 is required for S3 storage") from exc
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

    @staticmethod
    def _error_code(exc: Exception) -> str:
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            err = response.get("Error", {})
            if isinstance(err, dict):
                return str(err.get("Code", ""))
        return ""

    def ensure_bucket(self) -> None:
        """Create bucket if missing (safe to call repeatedly)."""
        try:
            self.client.head_bucket(Bucket=self.bucket_name)
            return
        except Exception as exc:
            code = self._error_code(exc)
            if code not in {"404", "NoSuchBucket"}:
                raise ObjectStorageError("Unable to check storage bucket") from exc

        kwargs: dict = {"Bucket": self.bucket_name}
        if self.region and self.region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
        self.client.create_bucket(**kwargs)
        logger.info("Created storage bucket: %s", self.bucket_name)

    def upload(self, key: str, data: bytes, content_type: str | None) -> None:
        kwargs: dict = {
            "Bucket": self.bucket_name,
            "Key": key,
            "Body": data,
        }
        if content_type:
            kwargs["ContentType"] = content_type
        try:
            self.client.put_object(**kwargs)
        except Exception as exc:
            raise ObjectStorageError("Failed to upload object") from exc

    def download(self, key: str) -> bytes:
        try:
            obj = self.client.get_object(Bucket=self.bucket_name, Key=key)
        except Exception as exc:
            code = self._error_code(exc)
            if code in {"404", "NoSuchKey"}:
                raise ObjectNotFoundError(key) from exc
            raise ObjectStorageError("Failed to download object") from exc
        return obj["Body"].read()

    def stream(self, key: str) -> StreamResult:
        try:
            obj = self.client.get_object(Bucket=self.bucket_name, Key=key)
        except Exception as exc:
            code = self._error_code(exc)
            if code in {"404", "NoSuchKey"}:
                raise ObjectNotFoundError(key) from exc
            raise ObjectStorageError("Failed to stream object") from exc

        body = obj["Body"]
        content_type = obj.get("ContentType")
        content_length = obj.get("ContentLength")
        return StreamResult(
            chunks=iter(lambda: body.read(1024 * 1024), b""),
            content_type=content_type,
            content_length=content_length,
        )

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except Exception as exc:
            code = self._error_code(exc)
            if code in {"404", "NoSuchKey"}:
                return False
            raise ObjectStorageError("Failed to check object") from exc

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket_name, Key=key)
        except Exception as exc:
            raise ObjectStorageError("Failed to delete object") from exc


@lru_cache(maxsize=1)
def get_s3_storage() -> S3StorageService:
    return S3StorageService(
        bucket_name=settings.s3_bucket_name,
        endpoint_url=settings.s3_endpoint_url,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        region=settings.s3_region,
    )


def ensure_storage_bucket() -> None:
    """Startup hook helper to guarantee bucket availability."""
    get_s3_storage().ensure_bucket()
