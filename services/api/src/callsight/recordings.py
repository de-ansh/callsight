"""Recording storage in MinIO or any S3-compatible service."""

from __future__ import annotations

from typing import BinaryIO

from minio import Minio

from .config import Settings


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
