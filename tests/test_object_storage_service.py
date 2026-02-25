from __future__ import annotations

from app.services.object_storage import S3StorageService


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
