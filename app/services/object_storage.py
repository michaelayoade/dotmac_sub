"""S3-compatible private object storage service."""

from __future__ import annotations

import io
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol
from urllib.parse import urlsplit

import urllib3

from app.config import settings

logger = logging.getLogger(__name__)

# Retry configuration for transient failures (DNS, network timeouts)
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BASE_DELAY = 1.0  # seconds
DEFAULT_RETRY_MAX_DELAY = 10.0  # seconds
DEFAULT_RETRY_EXPONENTIAL_BASE = 2.0
S3_CONNECT_TIMEOUT_SECONDS = 3.0
S3_READ_TIMEOUT_SECONDS = 10.0


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
        if isinstance(
            current, (socket.timeout, ConnectionRefusedError, ConnectionResetError)
        ):
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


class _ObjectResponse(Protocol):
    def read(self, size: int = -1) -> bytes: ...
    def close(self) -> None: ...
    def release_conn(self) -> None: ...


def _close_response(response: _ObjectResponse) -> None:
    """Release a response without replacing the read outcome with cleanup errors."""
    try:
        response.close()
    except Exception as exc:
        _warn_cleanup_failure("close", exc)
    try:
        response.release_conn()
    except Exception as exc:
        _warn_cleanup_failure("release", exc)


def _warn_cleanup_failure(action: str, exc: Exception) -> None:
    try:
        logger.warning("Storage response %s failed: %s", action, type(exc).__name__)
    except Exception:
        # Logging may already be unavailable when an abandoned iterator finalizes.
        pass


class _ResponseChunks(Iterator[bytes]):
    """Own an open object response from construction through abandonment."""

    def __init__(self, response: _ObjectResponse) -> None:
        self._response: _ObjectResponse | None = response

    def __iter__(self) -> _ResponseChunks:
        return self

    def __next__(self) -> bytes:
        response = self._response
        if response is None:
            raise StopIteration
        try:
            chunk = response.read(1024 * 1024)
        except Exception:
            self.close()
            raise
        if not chunk:
            self.close()
            raise StopIteration
        return chunk

    def close(self) -> None:
        response = self._response
        self._response = None
        if response is not None:
            _close_response(response)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            # Interpreter shutdown may already have discarded logging globals.
            pass


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
            from minio import Minio
        except ImportError as exc:
            raise ObjectStorageError("minio is required for S3 storage") from exc
        try:
            parsed = urlsplit(endpoint_url)
            host = parsed.hostname
            if (
                parsed.scheme not in {"http", "https"}
                or not host
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ObjectStorageError("Invalid storage endpoint URL")
            port = parsed.port
        except ValueError as exc:
            raise ObjectStorageError("Invalid storage endpoint URL") from exc
        endpoint = f"[{host}]" if ":" in host else host
        if port is not None:
            endpoint = f"{endpoint}:{port}"
        http_client = urllib3.PoolManager(
            maxsize=10,
            timeout=urllib3.Timeout(
                connect=S3_CONNECT_TIMEOUT_SECONDS,
                read=S3_READ_TIMEOUT_SECONDS,
            ),
            retries=False,
        )
        self.client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=parsed.scheme == "https",
            region=region,
            http_client=http_client,
        )

    @staticmethod
    def _error_code(exc: Exception) -> str:
        code = getattr(exc, "code", None)
        return code if isinstance(code, str) else ""

    def ensure_bucket(self) -> None:
        """Create bucket if missing (safe to call repeatedly)."""
        if self._bucket_ready:
            return
        try:
            exists = self.client.bucket_exists(self.bucket_name)
        except Exception as exc:
            raise ObjectStorageError("Unable to check storage bucket") from exc
        if not exists:
            try:
                self.client.make_bucket(self.bucket_name)
            except Exception as exc:
                # Another process may have created it after our existence check.
                try:
                    if not self.client.bucket_exists(self.bucket_name):
                        raise ObjectStorageError(
                            "Unable to create storage bucket"
                        ) from exc
                except ObjectStorageError:
                    raise
                except Exception as check_exc:
                    raise ObjectStorageError(
                        "Unable to check storage bucket"
                    ) from check_exc
            else:
                logger.info("Created storage bucket: %s", self.bucket_name)
        self._bucket_ready = True

    def upload(self, key: str, data: bytes, content_type: str | None) -> None:
        self.ensure_bucket()
        try:
            self.client.put_object(
                self.bucket_name,
                key,
                io.BytesIO(data),
                len(data),
                content_type=content_type or "application/octet-stream",
            )
        except Exception as exc:
            raise ObjectStorageError("Failed to upload object") from exc

    def download(self, key: str) -> bytes:
        try:
            response = self.client.get_object(self.bucket_name, key)
        except Exception as exc:
            code = self._error_code(exc)
            if code in {"404", "NoSuchKey", "NoSuchObject", "NotFound"}:
                raise ObjectNotFoundError(key) from exc
            raise ObjectStorageError("Failed to download object") from exc
        try:
            return response.read()
        except Exception as exc:
            raise ObjectStorageError("Failed to download object") from exc
        finally:
            _close_response(response)

    def stream(self, key: str) -> StreamResult:
        try:
            response = self.client.get_object(self.bucket_name, key)
        except Exception as exc:
            code = self._error_code(exc)
            if code in {"404", "NoSuchKey", "NoSuchObject", "NotFound"}:
                raise ObjectNotFoundError(key) from exc
            raise ObjectStorageError("Failed to stream object") from exc

        try:
            content_type = (
                response.headers["Content-Type"]
                if "Content-Type" in response.headers
                else None
            )
            length_header = (
                response.headers["Content-Length"]
                if "Content-Length" in response.headers
                else None
            )
            try:
                content_length = (
                    int(length_header) if length_header is not None else None
                )
            except ValueError:
                content_length = None
        except Exception as exc:
            _close_response(response)
            raise ObjectStorageError("Failed to stream object") from exc

        return StreamResult(
            chunks=_ResponseChunks(response),
            content_type=content_type,
            content_length=content_length,
        )

    def exists(self, key: str) -> bool:
        try:
            self.client.stat_object(self.bucket_name, key)
            return True
        except Exception as exc:
            code = self._error_code(exc)
            if code in {"404", "NoSuchKey", "NoSuchObject", "NotFound"}:
                return False
            raise ObjectStorageError("Failed to check object") from exc

    def delete(self, key: str) -> None:
        try:
            self.client.remove_object(self.bucket_name, key)
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
