"""S3-compatible private object storage service."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from app.config import settings

logger = logging.getLogger(__name__)

# Retry configuration for transient failures (DNS, network timeouts)
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BASE_DELAY = 1.0  # seconds
DEFAULT_RETRY_MAX_DELAY = 10.0  # seconds
DEFAULT_RETRY_EXPONENTIAL_BASE = 2.0


class ObjectStorageError(Exception):
    """Generic object storage failure."""


class ObjectNotFoundError(ObjectStorageError):
    """Raised when object is missing."""


class ObjectStorageConnectionError(ObjectStorageError):
    """Raised when storage connection fails (DNS, network, timeout)."""


def _is_transient_error(exc: Exception) -> bool:
    """Check if an exception is likely transient (worth retrying)."""
    import socket

    # Check the full exception chain (exc -> __cause__ -> __cause__.__cause__ etc)
    current: BaseException | None = exc
    while current is not None:
        # DNS resolution failures
        if isinstance(current, socket.gaierror):
            return True
        # Connection timeouts and refused connections
        if isinstance(current, (socket.timeout, ConnectionRefusedError, ConnectionResetError)):
            return True
        # OSError with network-related errno
        if isinstance(current, OSError) and current.errno in (
            101,  # Network is unreachable
            110,  # Connection timed out
            111,  # Connection refused
            113,  # No route to host
        ):
            return True
        current = current.__cause__

    # Check error message for transient patterns (covers wrapped exceptions)
    exc_str = str(exc).lower()
    transient_patterns = (
        "timeout",
        "timed out",
        "connection reset",
        "connection refused",
        "name or service not known",
        "temporary failure in name resolution",
        "network is unreachable",
        "no route to host",
        "endpoint connection error",
        "could not connect to the endpoint",
        "failed to resolve",
    )
    if any(pattern in exc_str for pattern in transient_patterns):
        return True
    return False


def _retry_with_backoff(
    operation: str,
    func,
    max_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    base_delay: float = DEFAULT_RETRY_BASE_DELAY,
    max_delay: float = DEFAULT_RETRY_MAX_DELAY,
    exponential_base: float = DEFAULT_RETRY_EXPONENTIAL_BASE,
):
    """
    Execute a function with exponential backoff retry for transient errors.

    Args:
        operation: Human-readable operation name for logging
        func: Callable to execute
        max_attempts: Maximum number of attempts (default: 3)
        base_delay: Initial delay between retries in seconds (default: 1.0)
        max_delay: Maximum delay between retries in seconds (default: 10.0)
        exponential_base: Base for exponential backoff (default: 2.0)

    Returns:
        Result of the function call

    Raises:
        ObjectStorageConnectionError: If all retries fail due to transient errors
        Exception: If a non-transient error occurs
    """
    last_exception: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except Exception as exc:
            last_exception = exc

            if not _is_transient_error(exc):
                # Non-transient error, don't retry
                raise

            if attempt == max_attempts:
                # Final attempt failed
                logger.error(
                    "Storage %s failed after %d attempts: %s",
                    operation,
                    max_attempts,
                    exc,
                )
                raise ObjectStorageConnectionError(
                    f"Storage {operation} failed after {max_attempts} attempts"
                ) from exc

            # Calculate delay with exponential backoff
            delay = min(base_delay * (exponential_base ** (attempt - 1)), max_delay)
            logger.warning(
                "Storage %s attempt %d/%d failed (%s), retrying in %.1fs",
                operation,
                attempt,
                max_attempts,
                type(exc).__name__,
                delay,
            )
            time.sleep(delay)

    # Should not reach here, but satisfy type checker
    if last_exception:
        raise last_exception
    raise ObjectStorageError(f"Storage {operation} failed unexpectedly")


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
        self._bucket_ready = False
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
        if self._bucket_ready:
            return
        try:
            self.client.head_bucket(Bucket=self.bucket_name)
            self._bucket_ready = True
            return
        except Exception as exc:
            code = self._error_code(exc)
            if code not in {"404", "NoSuchBucket"}:
                raise ObjectStorageError("Unable to check storage bucket") from exc

        kwargs: dict = {"Bucket": self.bucket_name}
        if self.region and self.region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
        self.client.create_bucket(**kwargs)
        self._bucket_ready = True
        logger.info("Created storage bucket: %s", self.bucket_name)

    def upload(self, key: str, data: bytes, content_type: str | None) -> None:
        self.ensure_bucket()
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


def ensure_storage_bucket(
    max_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    base_delay: float = DEFAULT_RETRY_BASE_DELAY,
    *,
    raise_on_failure: bool = True,
) -> bool:
    """
    Startup hook helper to guarantee bucket availability with retry logic.

    Retries transient failures (DNS resolution, network timeouts) with
    exponential backoff. This handles the common case where the app starts
    before the network stack or storage service is fully available.

    Args:
        max_attempts: Maximum retry attempts (default: 3)
        base_delay: Initial delay between retries in seconds (default: 1.0)
    """

    def _ensure() -> None:
        get_s3_storage().ensure_bucket()

    try:
        _retry_with_backoff(
            operation="bucket initialization",
            func=_ensure,
            max_attempts=max_attempts,
            base_delay=base_delay,
        )
        return True
    except ObjectStorageConnectionError:
        if raise_on_failure:
            raise
        logger.warning(
            "Storage bucket initialization deferred; object storage is currently unreachable"
        )
        return False
