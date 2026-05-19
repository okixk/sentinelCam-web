from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import settings

log = logging.getLogger("sentinelCam.storage")


_session: Optional[aioboto3.Session] = None
_bucket_ready = False
_bucket_lock = asyncio.Lock()


def _session_singleton() -> aioboto3.Session:
    global _session
    if _session is None:
        _session = aioboto3.Session(
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
        )
    return _session


def _client_kwargs() -> dict:
    return {
        "service_name": "s3",
        "endpoint_url": settings.s3_endpoint_url or None,
        "use_ssl": settings.s3_use_ssl,
        "config": Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    }


@asynccontextmanager
async def s3_client() -> AsyncIterator:
    session = _session_singleton()
    async with session.client(**_client_kwargs()) as client:
        yield client


def recording_key(user_id: int, filename: str) -> str:
    return f"recordings/{int(user_id)}/{filename}"


def thumbnail_key(user_id: int, source_stem: str) -> str:
    return f"recordings/{int(user_id)}/thumb_{source_stem}.jpg"


async def ensure_bucket() -> None:
    global _bucket_ready
    if _bucket_ready:
        return
    async with _bucket_lock:
        if _bucket_ready:
            return
        attempt = 0
        while True:
            attempt += 1
            try:
                async with s3_client() as client:
                    try:
                        await client.head_bucket(Bucket=settings.s3_bucket)
                    except ClientError as exc:
                        code = exc.response.get("Error", {}).get("Code", "")
                        if code in ("404", "NoSuchBucket", "NotFound"):
                            await client.create_bucket(Bucket=settings.s3_bucket)
                        else:
                            raise
                _bucket_ready = True
                log.info("Object storage bucket %r ready at %s", settings.s3_bucket, settings.s3_endpoint_url)
                return
            except (ClientError, OSError) as exc:
                if attempt >= 30:
                    raise
                log.warning("Object storage not ready (attempt %d): %s", attempt, exc)
                await asyncio.sleep(2.0)


async def put_bytes(key: str, data: bytes, content_type: str | None = None) -> None:
    extra: dict = {"Bucket": settings.s3_bucket, "Key": key, "Body": data}
    if content_type:
        extra["ContentType"] = content_type
    async with s3_client() as client:
        await client.put_object(**extra)


async def upload_stream(
    upload,
    key: str,
    max_bytes: int,
    sniff_bytes: int = 32,
    content_type: str | None = None,
) -> tuple[int, bytes]:
    """Streaming upload from a FastAPI UploadFile to S3, with a size cap and a
    short prefix returned for magic-byte sniffing."""
    from fastapi import HTTPException

    head = bytearray()
    total = 0
    chunks: list[bytes] = []

    try:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(413, "File too large")
            if len(head) < sniff_bytes:
                need = sniff_bytes - len(head)
                head.extend(chunk[:need])
            chunks.append(chunk)
    finally:
        await upload.close()

    body = b"".join(chunks)
    await put_bytes(key, body, content_type=content_type)
    return total, bytes(head)


async def get_bytes(key: str) -> bytes:
    async with s3_client() as client:
        try:
            resp = await client.get_object(Bucket=settings.s3_bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NotFound"):
                raise FileNotFoundError(key) from exc
            raise
        async with resp["Body"] as stream:
            return await stream.read()


async def head_object(key: str) -> Optional[dict]:
    async with s3_client() as client:
        try:
            resp = await client.head_object(Bucket=settings.s3_bucket, Key=key)
            return dict(resp)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise


async def object_exists(key: str) -> bool:
    return await head_object(key) is not None


async def stream_object(key: str, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
    """Async iterator that yields chunks for a stored object."""
    async with s3_client() as client:
        try:
            resp = await client.get_object(Bucket=settings.s3_bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NotFound"):
                raise FileNotFoundError(key) from exc
            raise
        async with resp["Body"] as body:
            while True:
                chunk = await body.read(chunk_size)
                if not chunk:
                    break
                yield chunk


async def delete_object(key: str) -> None:
    async with s3_client() as client:
        try:
            await client.delete_object(Bucket=settings.s3_bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NotFound"):
                return
            raise


async def delete_many(keys: list[str]) -> None:
    keys = [k for k in keys if k]
    if not keys:
        return
    async with s3_client() as client:
        try:
            await client.delete_objects(
                Bucket=settings.s3_bucket,
                Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True},
            )
        except ClientError as exc:
            log.warning("delete_objects failed (%s); falling back to per-key delete", exc)
            for k in keys:
                await delete_object(k)


def object_response_headers(s3_response: dict) -> dict[str, str]:
    headers: dict[str, str] = {}
    length = s3_response.get("ContentLength")
    if length is not None:
        headers["Content-Length"] = str(length)
    etag = s3_response.get("ETag")
    if etag:
        headers["ETag"] = etag
    return headers
