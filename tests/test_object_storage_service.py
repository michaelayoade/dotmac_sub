from __future__ import annotations

import gc
import socket
import sys
from types import SimpleNamespace
from typing import cast
from unittest.mock import ANY, MagicMock, patch

import pytest

from app.services.object_storage import (
    ObjectNotFoundError,
    ObjectStorageConnectionError,
    ObjectStorageError,
    S3StorageService,
    _is_transient_error,
    _ResponseChunks,
    _retry_with_backoff,
    ensure_storage_bucket,
)


class _ClientError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _FakeResponse:
    def __init__(self, data: bytes, content_type: str | None):
        self._data = data
        self.headers = {"Content-Length": str(len(data))}
        if content_type:
            self.headers["Content-Type"] = content_type
        self.closed = False
        self.released = False

    def read(self, size: int = -1) -> bytes:
        chunk = self._data[:size] if size >= 0 else self._data
        self._data = self._data[size:] if size >= 0 else b""
        return chunk

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class _FakeS3Client:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.created_bucket = False
        self.has_bucket = True
        self.content_types: dict[str, str] = {}
        self.responses: list[_FakeResponse] = []
        self.upload_content_types: list[str] = []

    def bucket_exists(self, bucket: str) -> bool:
        return self.has_bucket

    def make_bucket(self, bucket: str) -> None:
        self.created_bucket = True
        self.has_bucket = True

    def put_object(
        self, bucket: str, key: str, data, length: int, *, content_type: str
    ) -> None:
        payload = data.read()
        assert length == len(payload)
        self.objects[key] = payload
        self.content_types[key] = content_type
        self.upload_content_types.append(content_type)

    def get_object(self, bucket: str, key: str) -> _FakeResponse:
        if key not in self.objects:
            raise _ClientError("NoSuchKey")
        response = _FakeResponse(self.objects[key], self.content_types.get(key))
        self.responses.append(response)
        return response

    def stat_object(self, bucket: str, key: str) -> None:
        if key not in self.objects:
            raise _ClientError("NoSuchKey")

    def remove_object(self, bucket: str, key: str) -> None:
        self.objects.pop(key, None)


def test_bucket_creation_idempotent():
    fake = _FakeS3Client()
    fake.has_bucket = False
    service = S3StorageService(
        "bucket", "http://minio:9000", "a", "b", "us-east-1", client=fake
    )

    service.ensure_bucket()
    assert fake.created_bucket is True

    fake.created_bucket = False
    fake.has_bucket = True
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
    assert fake.responses[-1].closed and fake.responses[-1].released

    stream = service.stream("k/1.txt")
    assert b"".join(stream.chunks) == b"hello"
    assert stream.content_type == "text/plain"
    assert stream.content_length == 5
    assert fake.responses[-1].closed and fake.responses[-1].released

    service.delete("k/1.txt")
    assert service.exists("k/1.txt") is False


def test_upload_ensures_bucket_before_write():
    fake = _FakeS3Client()
    fake.has_bucket = False
    service = S3StorageService(
        "bucket", "http://minio:9000", "a", "b", "us-east-1", client=fake
    )

    service.upload("k/1.txt", b"hello", "text/plain")

    assert fake.created_bucket is True
    assert fake.objects["k/1.txt"] == b"hello"


def test_stream_releases_response_when_closed_early():
    fake = _FakeS3Client()
    fake.objects["large"] = b"x" * (1024 * 1024 + 1)
    service = S3StorageService(
        "bucket", "http://minio:9000", "a", "b", "us-east-1", client=fake
    )
    stream = service.stream("large")

    assert len(next(stream.chunks)) == 1024 * 1024
    cast(_ResponseChunks, stream.chunks).close()
    assert fake.responses[-1].closed and fake.responses[-1].released


def test_stream_releases_response_when_closed_before_first_read():
    fake = _FakeS3Client()
    fake.objects["k"] = b"data"
    service = S3StorageService(
        "bucket", "http://minio:9000", "a", "b", "us-east-1", client=fake
    )

    stream = service.stream("k")
    cast(_ResponseChunks, stream.chunks).close()
    assert fake.responses[-1].closed and fake.responses[-1].released
    assert list(stream.chunks) == []


def test_abandoned_stream_releases_response():
    fake = _FakeS3Client()
    fake.objects["k"] = b"data"
    service = S3StorageService(
        "bucket", "http://minio:9000", "a", "b", "us-east-1", client=fake
    )

    stream = service.stream("k")
    response = fake.responses[-1]
    del stream
    gc.collect()
    assert response.closed and response.released


def test_download_releases_response_after_read_failure(monkeypatch):
    fake = _FakeS3Client()
    response = _FakeResponse(b"data", "text/plain")
    monkeypatch.setattr(response, "read", MagicMock(side_effect=OSError("read failed")))
    monkeypatch.setattr(fake, "get_object", MagicMock(return_value=response))
    service = S3StorageService(
        "bucket", "http://minio:9000", "a", "b", "us-east-1", client=fake
    )

    with pytest.raises(ObjectStorageError, match="Failed to download object"):
        service.download("k")
    assert response.closed and response.released


def test_cleanup_failure_does_not_mask_read_error(monkeypatch):
    fake = _FakeS3Client()
    response = _FakeResponse(b"data", "text/plain")
    monkeypatch.setattr(response, "read", MagicMock(side_effect=OSError("read failed")))
    monkeypatch.setattr(
        response, "close", MagicMock(side_effect=OSError("close failed"))
    )
    monkeypatch.setattr(fake, "get_object", MagicMock(return_value=response))
    service = S3StorageService(
        "bucket", "http://minio:9000", "a", "b", "us-east-1", client=fake
    )

    with pytest.raises(ObjectStorageError, match="Failed to download object") as exc:
        service.download("k")
    assert isinstance(exc.value.__cause__, OSError)
    assert str(exc.value.__cause__) == "read failed"
    assert response.released


def test_stream_releases_response_after_read_failure(monkeypatch):
    fake = _FakeS3Client()
    response = _FakeResponse(b"data", "text/plain")
    monkeypatch.setattr(response, "read", MagicMock(side_effect=OSError("read failed")))
    monkeypatch.setattr(fake, "get_object", MagicMock(return_value=response))
    service = S3StorageService(
        "bucket", "http://minio:9000", "a", "b", "us-east-1", client=fake
    )

    with pytest.raises(OSError, match="read failed"):
        next(service.stream("k").chunks)
    assert response.closed and response.released


def test_missing_object_and_default_content_type():
    fake = _FakeS3Client()
    service = S3StorageService(
        "bucket", "http://minio:9000", "a", "b", "us-east-1", client=fake
    )

    assert service.exists("missing") is False
    with pytest.raises(ObjectNotFoundError):
        service.download("missing")
    with pytest.raises(ObjectNotFoundError):
        service.stream("missing")

    service.upload("plain", b"data", None)
    assert fake.upload_content_types == ["application/octet-stream"]


def test_minio_client_receives_parsed_endpoint(monkeypatch):
    client_factory = MagicMock()
    monkeypatch.setitem(sys.modules, "minio", SimpleNamespace(Minio=client_factory))
    service = S3StorageService(
        "bucket", "https://objects.example:9000/", "access", "secret", "eu-west-1"
    )

    assert service.client is client_factory.return_value
    client_factory.assert_called_once_with(
        "objects.example:9000",
        access_key="access",
        secret_key="secret",
        secure=True,
        region="eu-west-1",
        http_client=ANY,
    )
    http_client = client_factory.call_args.kwargs["http_client"]
    assert http_client.connection_pool_kw["maxsize"] == 10
    assert http_client.connection_pool_kw["timeout"].connect_timeout == 3.0
    assert http_client.connection_pool_kw["timeout"].read_timeout == 10.0
    assert http_client.connection_pool_kw["retries"].total is False


@pytest.mark.parametrize(
    "endpoint",
    [
        "objects.example:9000",
        "ftp://objects.example",
        "https://user@objects.example",
        "https://objects.example/path",
        "https://objects.example:bad",
    ],
)
def test_invalid_endpoint_is_rejected(monkeypatch, endpoint):
    monkeypatch.setitem(sys.modules, "minio", SimpleNamespace(Minio=MagicMock()))
    with pytest.raises(ObjectStorageError, match="Invalid storage endpoint URL"):
        S3StorageService("bucket", endpoint, "access", "secret", "us-east-1")


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
        func = MagicMock(side_effect=[socket.gaierror(8, "DNS failed"), "success"])
        result = _retry_with_backoff("test op", func, max_attempts=3, base_delay=1.0)
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
