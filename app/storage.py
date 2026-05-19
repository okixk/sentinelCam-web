from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from typing import Optional

from app.config import settings

log = logging.getLogger("sentinelCam.storage")


_storage_ready = False
_storage_lock = asyncio.Lock()


def _storage_root() -> Path:
    return Path(settings.local_storage_path).expanduser().resolve()


def _path_for_key(key: str) -> Path:
    if not key or "\\" in key:
        raise ValueError("Invalid storage key")

    key_path = PurePosixPath(key)
    if key_path.is_absolute():
        raise ValueError("Invalid storage key")

    parts = key_path.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError("Invalid storage key")

    root = _storage_root()
    target = root.joinpath(*parts).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("Invalid storage key") from exc
    return target


def _etag_from_stat(stat_result: os.stat_result) -> str:
    return f'"{stat_result.st_mtime_ns:x}-{stat_result.st_size:x}"'


def _object_metadata(path: Path) -> dict:
    stat_result = path.stat()
    return {
        "ContentLength": stat_result.st_size,
        "ETag": _etag_from_stat(stat_result),
        "LastModified": stat_result.st_mtime,
    }


def recording_key(user_id: int, filename: str) -> str:
    return f"recordings/{int(user_id)}/{filename}"


def thumbnail_key(user_id: int, source_stem: str) -> str:
    return f"recordings/{int(user_id)}/thumb_{source_stem}.jpg"


async def ensure_storage() -> None:
    global _storage_ready
    if _storage_ready:
        return
    async with _storage_lock:
        if _storage_ready:
            return
        root = _storage_root()
        await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
        _storage_ready = True
        log.info("Local recording storage ready at %s", root)


async def put_bytes(key: str, data: bytes, content_type: str | None = None) -> None:
    target = _path_for_key(key)

    def write_file() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp_path.write_bytes(data)
            os.replace(tmp_path, target)
        finally:
            tmp_path.unlink(missing_ok=True)

    await asyncio.to_thread(write_file)


async def upload_stream(
    upload,
    key: str,
    max_bytes: int,
    sniff_bytes: int = 32,
    content_type: str | None = None,
) -> tuple[int, bytes]:
    """Store an UploadFile on local disk with a size cap and sniffing prefix."""
    from fastapi import HTTPException

    target = _path_for_key(key)
    await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")

    head = bytearray()
    total = 0

    try:
        with tmp_path.open("wb") as handle:
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
                handle.write(chunk)
        await asyncio.to_thread(os.replace, tmp_path, target)
        return total, bytes(head)
    finally:
        await upload.close()
        await asyncio.to_thread(tmp_path.unlink, missing_ok=True)


async def get_bytes(key: str) -> bytes:
    path = _path_for_key(key)
    try:
        return await asyncio.to_thread(path.read_bytes)
    except FileNotFoundError:
        raise FileNotFoundError(key) from None


async def head_object(key: str) -> Optional[dict]:
    path = _path_for_key(key)

    def read_metadata() -> Optional[dict]:
        try:
            return _object_metadata(path)
        except FileNotFoundError:
            return None

    return await asyncio.to_thread(read_metadata)


async def object_exists(key: str) -> bool:
    return await head_object(key) is not None


async def stream_object(key: str, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
    """Async iterator that yields chunks for a stored object."""
    path = _path_for_key(key)
    if not await asyncio.to_thread(path.is_file):
        raise FileNotFoundError(key)

    with path.open("rb") as handle:
        while True:
            chunk = await asyncio.to_thread(handle.read, chunk_size)
            if not chunk:
                break
            yield chunk


async def delete_object(key: str) -> None:
    path = _path_for_key(key)
    await asyncio.to_thread(path.unlink, missing_ok=True)


async def delete_many(keys: list[str]) -> None:
    for key in [k for k in keys if k]:
        await delete_object(key)


def object_response_headers(object_metadata: dict) -> dict[str, str]:
    headers: dict[str, str] = {}
    length = object_metadata.get("ContentLength")
    if length is not None:
        headers["Content-Length"] = str(length)
    etag = object_metadata.get("ETag")
    if etag:
        headers["ETag"] = etag
    return headers
