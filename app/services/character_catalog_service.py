"""
캐릭터 카탈로그(DB 원본화) 전환용 공통 서비스.

[1차 목표]
- FRONT/public/characters/*.json -> character_catalog seed import
- 관리자 목록/수정/삭제 라우터의 공통 직렬화와 revision 기록

[주의]
- image 바이너리(media_assets) 업로드/저장은 Phase F 2차에서 붙인다.
- 현재는 image_url_external에 public 경로 또는 JSON의 image_url을 기록하는 브리지 단계다.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.character_catalog import CharacterCatalog
from app.models.character_catalog_revision import CharacterCatalogRevision
from app.services import character_catalog_image_service

logger = logging.getLogger(__name__)


def _repo_root() -> Optional[Path]:
    try:
        # app/services/character_catalog_service.py -> app/services -> app -> BACK -> repo root
        return Path(__file__).resolve().parents[3]
    except Exception:
        return None


def resolve_front_public_dir() -> Optional[Path]:
    root = _repo_root()
    if not root:
        return None
    d = root / "madcamp-Screening-Humanity-FRONT" / "public"
    return d if d.is_dir() else None


def resolve_front_public_characters_dir() -> Optional[Path]:
    public_dir = resolve_front_public_dir()
    if not public_dir:
        return None
    d = public_dir / "characters"
    return d if d.is_dir() else None


def resolve_front_public_character_images_dir() -> Optional[Path]:
    public_dir = resolve_front_public_dir()
    if not public_dir:
        return None
    d = public_dir / "images" / "characters"
    return d if d.is_dir() else None


def _slugify(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9가-힣_-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "character"


def _normalize_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, list):
        return [str(t).strip() for t in tags if str(t).strip()]
    if isinstance(tags, str):
        # 쉼표 구분 legacy 문자열도 허용
        parts = [p.strip() for p in tags.split(",")]
        return [p for p in parts if p]
    return []


def _pick_first_str(obj: Dict[str, Any], keys: Iterable[str]) -> Optional[str]:
    for key in keys:
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _guess_public_image_url(*, slug: str, json_obj: Dict[str, Any]) -> Optional[str]:
    explicit = _pick_first_str(json_obj, ["image_url", "image", "thumbnail_url"])
    if explicit:
        return explicit

    images_dir = resolve_front_public_character_images_dir()
    if not images_dir:
        return None

    for ext in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
        p = images_dir / f"{slug}{ext}"
        if p.exists():
            return f"/images/characters/{slug}{ext}"
    return None


def _guess_public_image_path(*, slug: str, json_obj: Dict[str, Any]) -> Optional[Path]:
    images_dir = resolve_front_public_character_images_dir()
    if not images_dir:
        return None

    explicit = _pick_first_str(json_obj, ["image_url", "image", "thumbnail_url"])
    if explicit and explicit.startswith("/images/characters/"):
        candidate = resolve_front_public_dir()
        if candidate:
            p = candidate / explicit.lstrip("/")
            if p.exists():
                return p

    for ext in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
        p = images_dir / f"{slug}{ext}"
        if p.exists():
            return p
    return None


def serialize_catalog_item(model: CharacterCatalog) -> Dict[str, Any]:
    image_public = (
        character_catalog_image_service.catalog_image_public_url(model.id)
        if model.image_asset_id
        else None
    )
    return {
        "id": model.id,
        "slug": model.slug,
        "name": model.name,
        "description": model.description,
        "persona": model.persona,
        "category": model.category,
        "tags": list(model.tags or []),
        "image_asset_id": model.image_asset_id,
        "image_url_external": model.image_url_external,
        "image_url": image_public or model.image_url_external,
        "image_url_canonical": image_public,
        "voice_id": model.voice_id,
        "origin_type": model.origin_type,
        "is_public": bool(model.is_public),
        "is_preset": bool(model.is_preset),
        "seed_source_path": model.seed_source_path,
        "created_by_user_id": model.created_by_user_id,
        "updated_by_user_id": model.updated_by_user_id,
        "created_at": model.created_at.isoformat() if model.created_at else None,
        "updated_at": model.updated_at.isoformat() if model.updated_at else None,
    }


def serialize_catalog_revision(model: CharacterCatalogRevision) -> Dict[str, Any]:
    return {
        "id": model.id,
        "character_id": model.character_id,
        "changed_by_user_id": model.changed_by_user_id,
        "action": model.action,
        "before_data": model.before_data,
        "after_data": model.after_data,
        "change_reason": model.change_reason,
        "created_at": model.created_at.isoformat() if model.created_at else None,
    }


async def _append_revision(
    *,
    db: AsyncSession,
    model: CharacterCatalog,
    action: str,
    changed_by_user_id: Optional[str],
    before_data: Optional[Dict[str, Any]],
    after_data: Optional[Dict[str, Any]],
    reason: Optional[str] = None,
) -> None:
    db.add(
        CharacterCatalogRevision(
            character_id=model.id,
            changed_by_user_id=changed_by_user_id,
            action=action,
            before_data=before_data,
            after_data=after_data,
            change_reason=reason,
        )
    )


def build_catalog_list_query(
    *,
    search: Optional[str] = None,
    origin_type: Optional[str] = None,
    is_public: Optional[bool] = None,
) -> Select[tuple[CharacterCatalog]]:
    q: Select[tuple[CharacterCatalog]] = select(CharacterCatalog)
    if search:
        like = f"%{search.strip()}%"
        q = q.where((CharacterCatalog.name.ilike(like)) | (CharacterCatalog.slug.ilike(like)))
    if origin_type:
        q = q.where(CharacterCatalog.origin_type == origin_type)
    if is_public is not None:
        q = q.where(CharacterCatalog.is_public == is_public)
    q = q.order_by(CharacterCatalog.updated_at.desc(), CharacterCatalog.created_at.desc())
    return q


async def list_catalog_items(
    *,
    db: AsyncSession,
    search: Optional[str] = None,
    origin_type: Optional[str] = None,
    is_public: Optional[bool] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    q = build_catalog_list_query(search=search, origin_type=origin_type, is_public=is_public).limit(max(1, min(limit, 500)))
    result = await db.execute(q)
    return [serialize_catalog_item(row) for row in result.scalars().all()]


async def list_public_preset_items_as_legacy_payload(
    *,
    db: AsyncSession,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """
    [역할]
    DB 캐릭터 카탈로그(preset/public)를 기존 `/api/characters/presets` 응답 shape에 맞춰 반환한다.

    [주의]
    - 1차 브리지 단계에서는 `slug`를 legacy `id`로 사용한다.
    - `image_url_external`을 legacy `image_url`로 매핑한다.
    """
    q = (
        select(CharacterCatalog)
        .where(CharacterCatalog.is_public.is_(True), CharacterCatalog.is_preset.is_(True))
        .order_by(CharacterCatalog.updated_at.desc(), CharacterCatalog.created_at.desc())
        .limit(max(1, min(limit, 1000)))
    )
    result = await db.execute(q)
    rows = result.scalars().all()
    out: List[Dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": row.slug or row.id,
                "name": row.name,
                "description": row.description,
                "persona": row.persona,
                "voice_id": row.voice_id,
                "category": row.category,
                "tags": list(row.tags or []),
                "image_url": (
                    character_catalog_image_service.catalog_image_public_url(row.id)
                    if row.image_asset_id
                    else row.image_url_external
                ),
                "image_url_canonical": (
                    character_catalog_image_service.catalog_image_public_url(row.id)
                    if row.image_asset_id
                    else None
                ),
                "is_preset": True,
                "_source": "db_character_catalog",
                "_catalog_id": row.id,
                "_slug": row.slug,
            }
        )
    return out


async def get_catalog_item_or_none(*, db: AsyncSession, catalog_id: str) -> Optional[CharacterCatalog]:
    result = await db.execute(select(CharacterCatalog).where(CharacterCatalog.id == catalog_id))
    return result.scalar_one_or_none()


async def update_catalog_item(
    *,
    db: AsyncSession,
    catalog: CharacterCatalog,
    patch: Dict[str, Any],
    user_id: Optional[str],
    before_data: Optional[Dict[str, Any]] = None,
) -> CharacterCatalog:
    before = before_data or serialize_catalog_item(catalog)
    for key in [
        "name",
        "slug",
        "description",
        "persona",
        "category",
        "image_asset_id",
        "image_url_external",
        "voice_id",
        "origin_type",
        "is_public",
        "is_preset",
    ]:
        if key in patch:
            setattr(catalog, key, patch[key])
    if "tags" in patch:
        catalog.tags = _normalize_tags(patch.get("tags"))
    catalog.updated_by_user_id = user_id
    await _append_revision(
        db=db,
        model=catalog,
        action="update",
        changed_by_user_id=user_id,
        before_data=before,
        after_data=serialize_catalog_item(catalog),
    )
    return catalog


async def delete_catalog_item(
    *,
    db: AsyncSession,
    catalog: CharacterCatalog,
    user_id: Optional[str],
) -> None:
    before = serialize_catalog_item(catalog)
    await _append_revision(
        db=db,
        model=catalog,
        action="delete",
        changed_by_user_id=user_id,
        before_data=before,
        after_data=None,
    )
    await db.delete(catalog)


async def list_catalog_revisions(
    *,
    db: AsyncSession,
    catalog_id: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    q = (
        select(CharacterCatalogRevision)
        .where(CharacterCatalogRevision.character_id == catalog_id)
        .order_by(CharacterCatalogRevision.created_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    result = await db.execute(q)
    return [serialize_catalog_revision(row) for row in result.scalars().all()]


def _load_seed_json_files(limit: Optional[int] = None) -> List[tuple[Path, Dict[str, Any]]]:
    base = resolve_front_public_characters_dir()
    if not base:
        return []
    pairs: List[tuple[Path, Dict[str, Any]]] = []
    count = 0
    for path in sorted(base.glob("*.json")):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                pairs.append((path, obj))
                count += 1
                if limit and count >= limit:
                    break
        except Exception as exc:
            logger.warning("character catalog seed JSON load failed path=%s err=%s", path, exc)
    return pairs


async def seed_import_from_front_public(
    *,
    db: AsyncSession,
    changed_by_user_id: Optional[str],
    dry_run: bool = False,
    limit: Optional[int] = None,
    upsert: bool = True,
) -> Dict[str, Any]:
    seed_pairs = _load_seed_json_files(limit=limit)
    scanned = len(seed_pairs)
    created = 0
    updated = 0
    skipped = 0
    image_candidates = 0
    image_missing = 0
    image_errors = 0
    image_blob = 0
    image_volume = 0

    items_preview: List[Dict[str, Any]] = []

    for path, obj in seed_pairs:
        stem = path.stem
        slug = _slugify(str(obj.get("id") or obj.get("slug") or stem))
        name = _pick_first_str(obj, ["name"]) or stem
        tags = _normalize_tags(obj.get("tags"))
        image_url = _guess_public_image_url(slug=slug, json_obj=obj)
        image_path = _guess_public_image_path(slug=slug, json_obj=obj)

        if image_path:
            image_candidates += 1
            if dry_run:
                try:
                    size = image_path.stat().st_size
                    if size <= character_catalog_image_service.IMAGE_BLOB_MAX_BYTES:
                        image_blob += 1
                    else:
                        image_volume += 1
                except Exception:
                    image_errors += 1
        else:
            image_missing += 1

        payload = {
            "slug": slug,
            "name": name,
            "description": _pick_first_str(obj, ["description"]),
            "persona": _pick_first_str(obj, ["persona"]),
            "category": _pick_first_str(obj, ["category"]),
            "tags": tags,
            "image_url_external": image_url,
            "voice_id": _pick_first_str(obj, ["voice_id"]),
            "origin_type": "seed_public",
            "is_public": True,
            "is_preset": True,
            "seed_source_path": str(path),
        }

        items_preview.append({"slug": slug, "name": name, "seed_source_path": str(path)})

        result = await db.execute(select(CharacterCatalog).where(CharacterCatalog.slug == slug))
        existing = result.scalar_one_or_none()
        if existing and not upsert:
            skipped += 1
            continue

        if dry_run:
            if existing:
                updated += 1
            else:
                created += 1
            continue

        if existing:
            before = serialize_catalog_item(existing)
            existing.name = payload["name"]
            existing.description = payload["description"]
            existing.persona = payload["persona"]
            existing.category = payload["category"]
            existing.tags = payload["tags"]
            existing.image_url_external = payload["image_url_external"]
            existing.voice_id = payload["voice_id"]
            existing.origin_type = "seed_public"
            existing.is_public = True
            existing.is_preset = True
            existing.seed_source_path = payload["seed_source_path"]
            existing.updated_by_user_id = changed_by_user_id
            if image_path:
                try:
                    _, stored_asset = await character_catalog_image_service.store_catalog_image_file(
                        db=db,
                        catalog=existing,
                        source_file_path=image_path,
                        owner_user_id=changed_by_user_id,
                    )
                    if stored_asset.storage_backend == "pg_blob":
                        image_blob += 1
                    else:
                        image_volume += 1
                except Exception as exc:
                    image_errors += 1
                    logger.warning("character catalog seed image upsert failed slug=%s path=%s err=%s", slug, image_path, exc)
            await _append_revision(
                db=db,
                model=existing,
                action="seed_import",
                changed_by_user_id=changed_by_user_id,
                before_data=before,
                after_data=serialize_catalog_item(existing),
                reason="FRONT/public seed upsert",
            )
            updated += 1
            continue

        model = CharacterCatalog(
            slug=payload["slug"],
            name=payload["name"],
            description=payload["description"],
            persona=payload["persona"],
            category=payload["category"],
            tags=payload["tags"],
            image_url_external=payload["image_url_external"],
            voice_id=payload["voice_id"],
            origin_type="seed_public",
            is_public=True,
            is_preset=True,
            seed_source_path=payload["seed_source_path"],
            created_by_user_id=changed_by_user_id,
            updated_by_user_id=changed_by_user_id,
        )
        db.add(model)
        # flush to get id for revision FK
        await db.flush()
        if image_path:
            try:
                _, stored_asset = await character_catalog_image_service.store_catalog_image_file(
                    db=db,
                    catalog=model,
                    source_file_path=image_path,
                    owner_user_id=changed_by_user_id,
                )
                if stored_asset.storage_backend == "pg_blob":
                    image_blob += 1
                else:
                    image_volume += 1
            except Exception as exc:
                image_errors += 1
                logger.warning("character catalog seed image insert failed slug=%s path=%s err=%s", slug, image_path, exc)
        await _append_revision(
            db=db,
            model=model,
            action="seed_import",
            changed_by_user_id=changed_by_user_id,
            before_data=None,
            after_data=serialize_catalog_item(model),
            reason="FRONT/public seed insert",
        )
        created += 1

    return {
        "scanned": scanned,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "dry_run": bool(dry_run),
        "upsert": bool(upsert),
        "items_preview": items_preview[:20],
        "source_dir": str(resolve_front_public_characters_dir()) if resolve_front_public_characters_dir() else None,
        "images_dir": str(resolve_front_public_character_images_dir()) if resolve_front_public_character_images_dir() else None,
        "image_stats": {
            "candidates": image_candidates,
            "missing": image_missing,
            "errors": image_errors,
            "stored_blob": image_blob,
            "stored_volume": image_volume,
        },
    }
