"""Recording storage in MinIO or any S3-compatible service."""

from __future__ import annotations

from typing import BinaryIO

from minio import Minio

from .config import Settings

AUDIO_TYPES = {
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
    ".flac": "audio/flac", ".webm": "audio/webm", ".mp4": "video/mp4",
}


def client(settings: Settings) -> Minio:
    return Minio(
        settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        secure=settings.s3_secure,
    )


def put(settings: Settings, key: str, data: BinaryIO, length: int, content_type: str) -> None:
    s3 = client(settings)
    if not s3.bucket_exists(settings.s3_bucket):
        s3.make_bucket(settings.s3_bucket)
    s3.put_object(settings.s3_bucket, key, data, length, content_type=content_type)


def download(settings: Settings, key: str, path: str) -> None:
    client(settings).fget_object(settings.s3_bucket, key, path)


def upload_file(settings: Settings, key: str, path: str, content_type: str) -> None:
    s3 = client(settings)
    if not s3.bucket_exists(settings.s3_bucket):
        s3.make_bucket(settings.s3_bucket)
    s3.fput_object(settings.s3_bucket, key, path, content_type=content_type)


def remove(settings: Settings, key: str) -> None:
    client(settings).remove_object(settings.s3_bucket, key)  # S3 deletes are idempotent


def size(settings: Settings, key: str) -> int:
    return client(settings).stat_object(settings.s3_bucket, key).size


def open_stream(settings: Settings, key: str, offset: int = 0, length: int = 0):
    """The stored object (or a byte range of it) as a stream; caller must close() and release_conn()."""
    return client(settings).get_object(settings.s3_bucket, key, offset=offset, length=length)


def parse_range(header: str | None, total: int) -> tuple[int, int] | None:
    """(start, end) inclusive for a single 'bytes=a-b' / 'bytes=a-' / 'bytes=-n' header; None = whole file.
    Raises ValueError for a range that cannot be satisfied."""
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    first, _, last = header[6:].strip().partition("-")
    if first == "":  # suffix range: the last n bytes
        n = int(last)
        if n <= 0:
            raise ValueError("empty range")
        return max(total - n, 0), total - 1
    start = int(first)
    end = min(int(last), total - 1) if last else total - 1
    if start > end or start >= total:
        raise ValueError("range not satisfiable")
    return start, end
