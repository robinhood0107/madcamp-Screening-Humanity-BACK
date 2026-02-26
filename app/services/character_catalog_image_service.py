from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
import re
from typing import Optional, TypedDict
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.core.config import settings
from app.models.character_catalog import CharacterCatalog
from app.models.media_asset import MediaAsset


IMAGE_BLOB_MAX_BYTES = 8 * 1024 * 1024  # 계획 확정 기본값 (<=8MB BLOB)


class CatalogImagePayload(TypedDict, total=False):
    storage_backend: str
    mime_type: str
    file_path: str
    blob_data: bytes
    public_url: str
    media_asset_id: str


def catalog_image_public_url(catalog_id: str) -> str:
    return f"/api/characters/catalog-image/{catalog_id}"


def _safe_name(name: Optional[str]) -> str:
    text = (name or "image").strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "image"


def _guess_mime_type(filename: Optional[str], explicit_mime: Optional[str]) -> str:
    if explicit_mime and "/" in explicit_mime:
        return explicit_mime
    guessed, _ = mimetypes.guess_type(filename or "")
    return guessed or "application/octet-stream"


def _guess_ext(filename: Optional[str], mime_type: str) -> str:
    if filename:
        suffix = Path(filename).suffix.strip()
        if suffix:
            return suffix.lower()
    ext = mimetypes.guess_extension(mime_type) or ".bin"
    return ext.lower()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _character_volume_root() -> Path:
    # 1차 브리지: backend `/assets` static mount(USER_ASSETS_DIR)를 재사용해 즉시 서빙 가능하게 한다.
    return Path(settings.USER_ASSETS_DIR) / "character_catalog"


async def _mark_previous_asset_orphaned_if_needed(*, db: AsyncSession, catalog: CharacterCatalog) -> None:
    if not catalog.image_asset_id:
        return
    res = await db.execute(select(MediaAsset).where(MediaAsset.id == catalog.image_asset_id))
    prev = res.scalar_one_or_none()
    if not prev:
        return
    prev.status = "orphaned"
    prev.deleted_at = func.now()


async def clear_catalog_image_asset(*, db: AsyncSession, catalog: CharacterCatalog) -> None:
    await _mark_previous_asset_orphaned_if_needed(db=db, catalog=catalog)
    catalog.image_asset_id = None


async def store_catalog_image_bytes(
    *,
    db: AsyncSession,
    catalog: CharacterCatalog,
    file_bytes: bytes,
    original_filename: Optional[str],
    mime_type: Optional[str],
    owner_user_id: Optional[str],
    change_reason: str = "character catalog image upload",
) -> tuple[CharacterCatalog, MediaAsset]:
    if not file_bytes:
        raise ValueError("empty image payload")

    resolved_mime = _guess_mime_type(original_filename, mime_type)
    ext = _guess_ext(original_filename, resolved_mime)
    size = len(file_bytes)
    sha256 = _sha256(file_bytes)
    filename_original = original_filename or f"{catalog.slug or catalog.id}{ext}"
    filename_stored = f"{catalog.id}_{uuid.uuid4().hex}{ext}"

    await _mark_previous_asset_orphaned_if_needed(db=db, catalog=catalog)

    asset = MediaAsset(
        owner_user_id=owner_user_id,
        owner_guest_session_id=None,
        asset_kind="character_image",
        storage_backend="pg_blob" if size <= IMAGE_BLOB_MAX_BYTES else "volume",
        mime_type=resolved_mime,
        size_bytes=size,
        sha256=sha256,
        filename_original=filename_original,
        filename_stored=filename_stored,
        status="active",
    )

    if asset.storage_backend == "pg_blob":
        asset.blob_data = file_bytes
    else:
        base_dir = _character_volume_root() / catalog.id
        _ensure_dir(base_dir)
        file_path = base_dir / filename_stored
        file_path.write_bytes(file_bytes)
        asset.volume_path = str(file_path)

    db.add(asset)
    await db.flush()

    catalog.image_asset_id = asset.id
    catalog.image_url_external = catalog_image_public_url(catalog.id)
    catalog.updated_at = func.now()

    return catalog, asset


async def store_catalog_image_file(
    *,
    db: AsyncSession,
    catalog: CharacterCatalog,
    source_file_path: Path,
    owner_user_id: Optional[str],
    original_filename: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> tuple[CharacterCatalog, MediaAsset]:
    file_bytes = source_file_path.read_bytes()
    return await store_catalog_image_bytes(
        db=db,
        catalog=catalog,
        file_bytes=file_bytes,
        original_filename=original_filename or source_file_path.name,
        mime_type=mime_type,
        owner_user_id=owner_user_id,
        change_reason="character catalog seed import image",
    )


async def load_catalog_image_payload(
    *,
    db: AsyncSession,
    catalog: CharacterCatalog,
) -> Optional[CatalogImagePayload]:
    if not catalog.image_asset_id:
        return None
    res = await db.execute(select(MediaAsset).where(MediaAsset.id == catalog.image_asset_id))
    asset = res.scalar_one_or_none()
    if not asset or asset.status != "active":
        return None

    payload: CatalogImagePayload = {
        "storage_backend": asset.storage_backend,
        "mime_type": asset.mime_type or "application/octet-stream",
        "media_asset_id": asset.id,
        "public_url": catalog_image_public_url(catalog.id),
    }
    if asset.storage_backend == "pg_blob" and asset.blob_data is not None:
        payload["blob_data"] = bytes(asset.blob_data)
        return payload

    if asset.storage_backend == "volume" and asset.volume_path:
        p = Path(asset.volume_path)
        if p.exists():
            payload["file_path"] = str(p)
            return payload
    return None
