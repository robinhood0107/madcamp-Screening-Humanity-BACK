from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin
from app.models.character_catalog import CharacterCatalog
from app.models.user import User
from app.services import character_catalog_image_service, character_catalog_service, character_service

router = APIRouter()
logger = logging.getLogger(__name__)


class CharacterCatalogUpdateBody(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    persona: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    image_url_external: Optional[str] = None
    voice_id: Optional[str] = None
    origin_type: Optional[str] = None
    is_public: Optional[bool] = None
    is_preset: Optional[bool] = None


class SeedImportRequest(BaseModel):
    dry_run: bool = False
    upsert: bool = True
    limit: Optional[int] = Field(default=None, ge=1, le=500)


async def _get_catalog_or_404(*, db: AsyncSession, catalog_id: str) -> CharacterCatalog:
    item = await character_catalog_service.get_catalog_item_or_none(db=db, catalog_id=catalog_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="캐릭터 카탈로그 항목을 찾을 수 없습니다")
    return item


@router.get("/admin/character-catalog")
async def list_admin_character_catalog(
    search: Optional[str] = None,
    origin_type: Optional[str] = None,
    is_public: Optional[bool] = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """관리자: DB 원본 캐릭터 카탈로그 목록 조회 (seed_public/admin_created/user_created 포함)."""
    try:
        items = await character_catalog_service.list_catalog_items(
            db=db,
            search=search,
            origin_type=origin_type,
            is_public=is_public,
            limit=limit,
        )
        return {"success": True, "data": {"items": items, "total": len(items)}}
    except SQLAlchemyError as exc:
        logger.exception("Admin character catalog list failed")
        raise HTTPException(status_code=500, detail=f"캐릭터 카탈로그 조회 실패: {exc}") from exc


@router.get("/admin/character-catalog/{catalog_id}/revisions")
async def list_admin_character_catalog_revisions(
    catalog_id: str,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """관리자: 카탈로그 항목 변경 이력 조회 (읽기 전용, 최신순)."""
    try:
        _ = current_user  # explicit admin dependency use
        await _get_catalog_or_404(db=db, catalog_id=catalog_id)
        items = await character_catalog_service.list_catalog_revisions(
            db=db,
            catalog_id=catalog_id,
            limit=limit,
        )
        return {"success": True, "data": {"items": items, "total": len(items)}}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Admin character catalog revisions list failed id=%s", catalog_id)
        raise HTTPException(status_code=500, detail=f"캐릭터 카탈로그 이력 조회 실패: {exc}") from exc


@router.post("/admin/character-catalog/seed-import")
async def seed_import_character_catalog(
    body: SeedImportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """관리자: FRONT/public/characters seed import를 DB 카탈로그로 가져온다. (1차: image_url_external 중심)"""
    try:
        result = await character_catalog_service.seed_import_from_front_public(
            db=db,
            changed_by_user_id=current_user.id,
            dry_run=body.dry_run,
            limit=body.limit,
            upsert=body.upsert,
        )
        if not body.dry_run:
            await db.commit()
        else:
            await db.rollback()
        return {"success": True, "data": result}
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        logger.exception("Admin character catalog seed import failed")
        raise HTTPException(status_code=500, detail=f"캐릭터 seed import 실패: {exc}") from exc


@router.put("/admin/character-catalog/{catalog_id}")
async def update_admin_character_catalog_item(
    catalog_id: str,
    body: CharacterCatalogUpdateBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """관리자: 카탈로그 항목 메타 수정."""
    try:
        item = await _get_catalog_or_404(db=db, catalog_id=catalog_id)
        patch = body.model_dump(exclude_unset=True)
        updated = await character_catalog_service.update_catalog_item(
            db=db,
            catalog=item,
            patch=patch,
            user_id=current_user.id,
        )
        await db.commit()
        await db.refresh(updated)
        return {"success": True, "data": character_catalog_service.serialize_catalog_item(updated)}
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        logger.exception("Admin character catalog update failed id=%s", catalog_id)
        raise HTTPException(status_code=500, detail=f"캐릭터 카탈로그 수정 실패: {exc}") from exc


@router.delete("/admin/character-catalog/{catalog_id}")
async def delete_admin_character_catalog_item(
    catalog_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """관리자: 카탈로그 항목 삭제 (1차: hard delete + revision 기록)."""
    try:
        item = await _get_catalog_or_404(db=db, catalog_id=catalog_id)
        await character_catalog_service.delete_catalog_item(db=db, catalog=item, user_id=current_user.id)
        await db.commit()
        return {"success": True, "data": {"message": "캐릭터 카탈로그 항목이 삭제되었습니다", "id": catalog_id}}
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        logger.exception("Admin character catalog delete failed id=%s", catalog_id)
        raise HTTPException(status_code=500, detail=f"캐릭터 카탈로그 삭제 실패: {exc}") from exc


@router.post("/admin/character-catalog/{catalog_id}/persona/generate")
async def generate_admin_character_catalog_persona(
    catalog_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """관리자: 카탈로그 항목의 페르소나/설명/태그를 AI로 생성하고 DB에 반영한다."""
    try:
        item = await _get_catalog_or_404(db=db, catalog_id=catalog_id)

        mock_data = {
            "persona": (
                f"성격: {item.name}은 신중하고 상황 판단이 빠릅니다.\n"
                "말투: 상황에 따라 차분하지만 필요할 때 단호합니다.\n"
                "배경: 현재 설정을 바탕으로 확장 가능한 서사를 가진 인물입니다.\n"
                "목표: 갈등을 해결하고 자신만의 선택을 관철하려고 합니다."
            ),
            "description": item.description or f"{item.name} 캐릭터의 상세 설정입니다.",
            "tags": [item.category or "일반", "AI생성", "카탈로그"],
            "category": item.category or "일반",
        }

        prompt = (
            f"다음 캐릭터의 상세 페르소나를 JSON으로 생성해줘.\n"
            f"- 이름: {item.name}\n"
            f"- 현재 설명: {item.description or '(없음)'}\n"
            f"- 현재 카테고리: {item.category or '(없음)'}\n"
            f"- 기존 페르소나(참고): {item.persona or '(없음)'}\n\n"
            "반드시 JSON만 반환하고 다음 필드를 포함해줘:\n"
            "- persona: 성격/말투/배경/목표 형식의 상세 페르소나 (200자 이상)\n"
            "- description: 1~2문장 요약 설명\n"
            "- tags: 문자열 배열 3~6개\n"
            "- category: 카테고리 문자열(없으면 '일반')\n"
        )
        messages = [{"role": "user", "content": prompt}]

        is_fallback = False
        try:
            content, is_fallback = await character_service.call_llm_json_with_gemini_fallback(
                messages=messages,
                system_instruction=None,
                fallback_model="glm-4.7-flash",
                temperature=0.7,
                max_tokens=1600,
                logger_hint="Admin character catalog persona generate",
            )
            generated = character_service.extract_json_object_from_llm_text(content)
            character_service.normalize_string_list_fields(generated, ["tags"])
        except Exception as exc:
            logger.warning("Admin character catalog persona generate fallback to mock data id=%s err=%s", catalog_id, exc)
            generated = mock_data
            is_fallback = True

        patch: Dict[str, Any] = {}
        if isinstance(generated.get("persona"), str) and generated["persona"].strip():
            patch["persona"] = generated["persona"].strip()
        if isinstance(generated.get("description"), str):
            patch["description"] = generated["description"].strip() or None
        if generated.get("tags") is not None:
            patch["tags"] = generated.get("tags")
        if isinstance(generated.get("category"), str):
            patch["category"] = generated["category"].strip() or None

        if not patch:
            raise HTTPException(status_code=500, detail="생성 결과에 저장 가능한 필드가 없습니다")

        updated = await character_catalog_service.update_catalog_item(
            db=db,
            catalog=item,
            patch=patch,
            user_id=current_user.id,
        )
        await db.commit()
        await db.refresh(updated)
        return {
            "success": True,
            "data": {
                "catalog": character_catalog_service.serialize_catalog_item(updated),
                "generated": {
                    "persona": generated.get("persona"),
                    "description": generated.get("description"),
                    "tags": generated.get("tags") or [],
                    "category": generated.get("category"),
                },
                "is_fallback": bool(is_fallback),
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        logger.exception("Admin character catalog persona generate failed id=%s", catalog_id)
        raise HTTPException(status_code=500, detail=f"캐릭터 페르소나 생성 실패: {exc}") from exc


@router.post("/admin/character-catalog/{catalog_id}/image")
async def upload_or_link_admin_character_catalog_image(
    catalog_id: str,
    image: Optional[UploadFile] = File(default=None),
    image_url_external: Optional[str] = Form(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """
    관리자: 카탈로그 이미지 업로드/링크 지정.

    1차 확장:
    - 링크 저장(image_url_external)
    - 파일 업로드(media_assets 하이브리드 저장 + character_catalog.image_asset_id 연결)
    """
    try:
        if image and image_url_external:
            raise HTTPException(status_code=400, detail="이미지 파일 업로드와 링크 지정은 동시에 사용할 수 없습니다")

        item = await _get_catalog_or_404(db=db, catalog_id=catalog_id)
        before = character_catalog_service.serialize_catalog_item(item)

        if image is not None:
            file_bytes = await image.read()
            if not file_bytes:
                raise HTTPException(status_code=400, detail="빈 이미지 파일은 업로드할 수 없습니다")

            await character_catalog_image_service.store_catalog_image_bytes(
                db=db,
                catalog=item,
                file_bytes=file_bytes,
                original_filename=image.filename,
                mime_type=image.content_type,
                owner_user_id=current_user.id,
            )
            updated = await character_catalog_service.update_catalog_item(
                db=db,
                catalog=item,
                patch={
                    "image_asset_id": item.image_asset_id,
                    "image_url_external": item.image_url_external,
                },
                user_id=current_user.id,
                before_data=before,
            )
            await db.commit()
            await db.refresh(updated)
            return {
                "success": True,
                "data": {
                    "catalog": character_catalog_service.serialize_catalog_item(updated),
                },
            }

        if image_url_external is None:
            raise HTTPException(status_code=400, detail="image_url_external 또는 image 파일이 필요합니다")

        normalized = image_url_external.strip()
        # 링크 지정/제거 시 기존 media_assets 연결은 orphan 처리하고 해제한다.
        if item.image_asset_id:
            await character_catalog_image_service.clear_catalog_image_asset(db=db, catalog=item)
        updated = await character_catalog_service.update_catalog_item(
            db=db,
            catalog=item,
            patch={
                "image_asset_id": None,
                "image_url_external": normalized or None,
            },
            user_id=current_user.id,
            before_data=before,
        )
        await db.commit()
        await db.refresh(updated)
        return {
            "success": True,
            "data": {
                "catalog": character_catalog_service.serialize_catalog_item(updated),
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        logger.exception("Admin character catalog image update failed id=%s", catalog_id)
        raise HTTPException(status_code=500, detail=f"캐릭터 이미지 저장 실패: {exc}") from exc
