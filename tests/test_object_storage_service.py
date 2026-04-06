from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest

from app.services.object_storage import (
    ObjectStorageConnectionError,
    S3StorageService,
    _is_transient_error,
    _retry_with_backoff,
    ensure_storage_bucket,
)


class _ClientError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeBody:
    def __init__(self, data: bytes):
        self._data = data
        self._read = False

    def read(self, size: int = -1) -> bytes:
        if size == -1:
            if self._read:
                return b""
            self._read = True
            return self._data
        if not self._data:
            return b""
        chunk = self._data[:size]
        self._data = self._data[size:]
        return chunk


class _FakeS3Client:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.created_bucket = False
        self.bucket_exists = True
        self.content_types: dict[str, str] = {}

    def head_bucket(self, Bucket: str):
        if self.bucket_exists:
            return {}
        raise _ClientError("404")

    def create_bucket(self, **kwargs):
        self.created_bucket = True

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str | None = None):
        self.objects[Key] = Body
        if ContentType:
            self.content_types[Key] = ContentType

    def get_object(self, Bucket: str, Key: str):
        if Key not in self.objects:
            raise _ClientError("NoSuchKey")
        return {
            "Body": _FakeBody(self.objects[Key]),
            "ContentType": self.content_types.get(Key),
            "ContentLength": len(self.objects[Key]),
        }

    def head_object(self, Bucket: str, Key: str):
        if Key not in self.objects:
            raise _ClientError("404")
        return {}

    def delete_object(self, Bucket: str, Key: str):
        self.objects.pop(Key, None)


def test_bucket_creation_idempotent():
    fake = _FakeS3Client()
    fake.bucket_exists = False
    service = S3StorageService(
        "bucket", "http://minio:9000", "a", "b", "us-east-1", client=fake
    )

    service.ensure_bucket()
    assert fake.created_bucket is True

    fake.created_bucket = False
    fake.bucket_exists = True
    service.ensure_bucket()
    assert fake.created_bucket is False


def test_upload_download_stream_exists_delete():
    fake = _FakeS3Client()
    service = S3StorageService(
        "bucket", "http://minio:9000", "a", "b", "us-east-1", client=fake
    )

    service.upload("k/1.txt", b"hello", "text/plain")
    assert service.exists("k/1.txt") is True
    assert service.download("k/1.txt") == b"hello"

    stream = service.stream("k/1.txt")
    assert b"".join(stream.chunks) == b"hello"
    assert stream.content_type == "text/plain"
    assert stream.content_length == 5

    service.delete("k/1.txt")
    assert service.exists("k/1.txt") is False


def test_upload_ensures_bucket_before_write():
    fake = _FakeS3Client()
    fake.bucket_exists = False
    service = S3StorageService(
        "bucket", "http://minio:9000", "a", "b", "us-east-1", client=fake
    )

    service.upload("k/1.txt", b"hello", "text/plain")

    assert fake.created_bucket is True
    assert fake.objects["k/1.txt"] == b"hello"


def test_ensure_storage_bucket_can_defer_connection_failures(monkeypatch):
    monkeypatch.setattr(
        "app.services.object_storage.get_s3_storage",
        MagicMock(
            return_value=MagicMock(
                ensure_bucket=MagicMock(
                    side_effect=ObjectStorageConnectionError("storage unavailable")
                )
            )
        ),
    )

    assert ensure_storage_bucket(raise_on_failure=False) is False


class TestTransientErrorDetection:
    """Tests for _is_transient_error function."""

    def test_dns_resolution_failure_is_transient(self):
        exc = socket.gaierror(8, "Name or service not known")
        assert _is_transient_error(exc) is True

    def test_socket_timeout_is_transient(self):
        exc = TimeoutError("timed out")
        assert _is_transient_error(exc) is True

    def test_connection_refused_is_transient(self):
        exc = ConnectionRefusedError("Connection refused")
        assert _is_transient_error(exc) is True

    def test_connection_reset_is_transient(self):
        exc = ConnectionResetError("Connection reset by peer")
        assert _is_transient_error(exc) is True

    def test_network_unreachable_oserror_is_transient(self):
        exc = OSError(101, "Network is unreachable")
        assert _is_transient_error(exc) is True

    def test_timeout_in_message_is_transient(self):
        exc = Exception("Connection timed out while connecting")
        assert _is_transient_error(exc) is True

    def test_temporary_dns_failure_in_message_is_transient(self):
        exc = Exception("Temporary failure in name resolution")
        assert _is_transient_error(exc) is True

    def test_value_error_is_not_transient(self):
        exc = ValueError("Invalid bucket name")
        assert _is_transient_error(exc) is False

    def test_permission_error_is_not_transient(self):
        exc = PermissionError("Access denied")
        assert _is_transient_error(exc) is False

    def test_generic_exception_is_not_transient(self):
        exc = Exception("Some other error")
        assert _is_transient_error(exc) is False

    def test_wrapped_dns_error_in_chain_is_transient(self):
        """Test that DNS errors wrapped in exception chains are detected."""
        # Simulates: ObjectStorageError <- EndpointConnectionError <- gaierror
        inner = socket.gaierror(8, "Name or service not known")
        middle = Exception("Could not connect")
        middle.__cause__ = inner
        outer = Exception("Unable to check storage bucket")
        outer.__cause__ = middle
        assert _is_transient_error(outer) is True

    def test_endpoint_connection_error_message_is_transient(self):
        exc = Exception("Could not connect to the endpoint URL")
        assert _is_transient_error(exc) is True


class TestRetryWithBackoff:
    """Tests for _retry_with_backoff function."""

    def test_success_on_first_attempt(self):
        func = MagicMock(return_value="success")
        result = _retry_with_backoff("test op", func, max_attempts=3)
        assert result == "success"
        assert func.call_count == 1

    @patch("app.services.object_storage.time.sleep")
    def test_retry_on_transient_error_then_success(self, mock_sleep):
        func = MagicMock(
            side_effect=[socket.gaierror(8, "DNS failed"), "success"]
        )
        result = _retry_with_backoff(
            "test op", func, max_attempts=3, base_delay=1.0
        )
        assert result == "success"
        assert func.call_count == 2
        mock_sleep.assert_called_once_with(1.0)

    @patch("app.services.object_storage.time.sleep")
    def test_exponential_backoff_delays(self, mock_sleep):
        func = MagicMock(
            side_effect=[
                socket.gaierror(8, "DNS failed"),
                TimeoutError("timeout"),
                "success",
            ]
        )
        result = _retry_with_backoff(
            "test op", func, max_attempts=3, base_delay=1.0, exponential_base=2.0
        )
        assert result == "success"
        assert func.call_count == 3
        # First retry: 1.0 * 2^0 = 1.0
        # Second retry: 1.0 * 2^1 = 2.0
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1.0)
        mock_sleep.assert_any_call(2.0)

    @patch("app.services.object_storage.time.sleep")
    def test_max_delay_cap(self, mock_sleep):
        func = MagicMock(
            side_effect=[
                socket.gaierror(8, "DNS failed"),
                socket.gaierror(8, "DNS failed"),
                "success",
            ]
        )
        result = _retry_with_backoff(
            "test op",
            func,
            max_attempts=3,
            base_delay=10.0,
            max_delay=5.0,
            exponential_base=2.0,
        )
        assert result == "success"
        # Both delays should be capped at max_delay=5.0
        mock_sleep.assert_any_call(5.0)

    @patch("app.services.object_storage.time.sleep")
    def test_raises_connection_error_after_max_attempts(self, mock_sleep):
        func = MagicMock(side_effect=socket.gaierror(8, "DNS failed"))
        with pytest.raises(ObjectStorageConnectionError) as exc_info:
            _retry_with_backoff("bucket init", func, max_attempts=3, base_delay=0.1)
        assert "bucket init failed after 3 attempts" in str(exc_info.value)
        assert func.call_count == 3

    def test_non_transient_error_not_retried(self):
        func = MagicMock(side_effect=ValueError("Invalid parameter"))
        with pytest.raises(ValueError, match="Invalid parameter"):
            _retry_with_backoff("test op", func, max_attempts=3)
        assert func.call_count == 1
