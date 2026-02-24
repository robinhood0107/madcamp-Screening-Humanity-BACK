from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import get_db, get_current_user, require_admin
from app.models.user import User
from app.models.character import Character
from app.services import character_service

router = APIRouter()
logger = logging.getLogger(__name__)


def _resolve_preset_characters_dir():
    """
    [역할]
    레거시 라우터 호환용 wrapper. 실제 경로 해석은 `character_service`가 담당한다.

    [왜 여기서 남겨두나]
    - `characters.py` 내부 및 기존 문서/분석 문서에서 함수명을 직접 참조하고 있어
      1차에서는 wrapper를 유지하는 쪽이 안전하다.
    """
    return character_service.resolve_preset_characters_dir()


def load_preset_characters() -> List[Dict[str, Any]]:
    """
    [역할]
    레거시 라우터/다른 모듈 호환용 wrapper. 실제 스캔/캐시는 `character_service`가 담당한다.
    """
    return character_service.load_preset_characters_from_disk()


def _serialize_character_response_data(character: Character) -> Dict[str, Any]:
    """
    [역할]
    레거시 라우터 호환용 wrapper. 실제 직렬화 규칙은 `character_service`가 담당한다.

    [왜 여기서 남겨두나]
    - `characters.py`에서 응답 dict 생성 의도를 바로 읽을 수 있게 하면서,
      구현 중복은 서비스로 몰아 재사용성을 높이기 위해서다.
    """
    return character_service.serialize_character_model(character)


def _ensure_not_preset_character(
    *,
    preset_chars: List[Dict[str, Any]],
    character_id: str,
    error_message: str,
) -> None:
    """
    [역할]
    preset 캐릭터에 대해 수정/삭제가 금지된 경로에서 공통 차단을 수행한다.
    """
    preset_char = character_service.find_preset_character_by_id(preset_chars, character_id)
    if preset_char:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error_message,
        )


async def _get_db_character_or_404(*, db: AsyncSession, character_id: str) -> Character:
    """
    [역할]
    DB 캐릭터를 조회하고 없으면 404를 발생시킨다.
    """
    result = await db.execute(select(Character).where(Character.id == character_id))
    character = result.scalar_one_or_none()
    if not character:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="캐릭터를 찾을 수 없습니다",
        )
    return character


async def _get_owned_character_or_raise(
    *,
    db: AsyncSession,
    character_id: str,
    current_user: User,
    forbidden_message: str = "캐릭터를 수정할 권한이 없습니다",
) -> Character:
    """
    [역할]
    DB 캐릭터 조회 + 본인 소유 권한 확인을 공통화한다.
    """
    character = await _get_db_character_or_404(db=db, character_id=character_id)
    if character.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=forbidden_message,
        )
    return character


def _raise_bad_request(detail_prefix: str, exc: Exception, *, log_context: str) -> None:
    """
    [역할]
    입력/검증 계열 예외를 400으로 고정 매핑한다.

    [왜 helper로 두나]
    - `characters.py`의 라우터들이 같은 형식(`detail`)을 반복 사용해서,
      예외 정책을 한 곳에서 맞추면 보수적 리팩토링 중 응답 shape를 덜 흔든다.
    """
    logger.warning("%s (bad request): %s", log_context, exc)
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"{detail_prefix}: {str(exc)}",
    )


def _raise_internal_server_error(detail_prefix: str, exc: Exception, *, log_context: str) -> None:
    """
    [역할]
    내부 오류를 500 + 기존 detail 문자열 형식으로 매핑한다.

    [주의]
    외부 계약 호환을 위해 `detail` 문구 형식(`{prefix}: {str(exc)}`)은 유지한다.
    """
    logger.exception("%s", log_context)
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"{detail_prefix}: {str(exc)}",
    )


async def _rollback_and_raise_internal_server_error(
    *,
    db: AsyncSession,
    detail_prefix: str,
    exc: Exception,
    log_context: str,
) -> None:
    """
    [역할]
    변경 라우트(create/update/delete/admin patch)에서 rollback + 500 매핑을 공통화한다.
    """
    try:
        await db.rollback()
    except Exception as rollback_error:
        # rollback 실패는 원래 예외를 덮지 않도록 경고만 남긴다.
        logger.warning("DB rollback failed while handling %s: %s", log_context, rollback_error)
    _raise_internal_server_error(detail_prefix, exc, log_context=log_context)

# Pydantic 모델
class CharacterBase(BaseModel):
    name: str = Field(..., description="캐릭터 이름")
    description: Optional[str] = Field(None, description="캐릭터 설명")
    persona: Optional[str] = Field(None, description="캐릭터 페르소나")
    voice_id: Optional[str] = Field(None, description="음성 ID")
    category: Optional[str] = Field(None, description="카테고리")
    tags: Optional[List[str]] = Field(None, description="태그 목록")
    image_url: Optional[str] = Field(None, description="이미지 URL")

class CharacterCreate(CharacterBase):
    pass

class CharacterUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    persona: Optional[str] = None
    voice_id: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    image_url: Optional[str] = None

class GenerateRequest(BaseModel):
    name: str
    description: Optional[str] = ""
    category: Optional[str] = ""


# 관리자: 캐릭터–Voice 연결/교체용
class AdminCharacterListItem(BaseModel):
    id: str
    name: str
    voice_id: Optional[str] = None
    is_preset: bool
    user_id: Optional[str] = None


class AdminCharacterVoiceUpdate(BaseModel):
    voice_id: Optional[str] = None


# ----- 공개/개발용 엔드포인트 -----
# [구간 의도]
# - preset 조회, dev-user 기반 CRUD처럼 운영 인증 없이 접근 가능한(또는 개발 편의 목적의) 경로를 모아둔다.
# - 운영 환경에서는 비활성화 후보가 될 수 있으므로 사용자 인증 라우트와 섞이지 않게 섹션으로 구분한다.
@router.get("/characters/presets")
async def list_preset_characters():
    """사전설정 캐릭터 목록 조회"""
    try:
        preset_chars = load_preset_characters()
        return {
            "success": True,
            "data": {
                "characters": preset_chars
            }
        }
    except (FileNotFoundError, OSError, TypeError, ValueError) as e:
        # `character_service.load_preset_characters_from_disk()`가 대부분의 파일/JSON 오류를 내부에서 빈 리스트로 흡수한다.
        # 여기서는 남아 있는 타입/직렬화 계열 오류만 기존 500 shape로 매핑한다.
        _raise_internal_server_error(
            "사전설정 캐릭터 조회 실패",
            e,
            log_context="Preset character list failed (typed residual error)",
        )

@router.get("/characters")
async def list_user_characters(
    db: AsyncSession = Depends(get_db)
):
    """
    [DEVELOPMENT ONLY] 개발 환경: 인증 없이 캐릭터 목록 조회 (dev-user)
    
    WARNING: 이 엔드포인트는 개발 및 디버깅 목적으로만 사용해야 합니다.
    실제 서비스에서는 보안을 위해 비활성화하거나 제거해야 합니다.
    """
    try:
        # 개발 환경: dev-user의 캐릭터만 조회
        result = await db.execute(
            select(Character).where(
                Character.user_id == "dev-user",
                Character.is_preset == False
            ).order_by(Character.created_at.desc())
        )
        characters = result.scalars().all()
        
        character_list = [_serialize_character_response_data(char) for char in characters]
        
        return {
            "success": True,
            "data": {
                "characters": character_list
            }
        }
    except SQLAlchemyError as e:
        _raise_internal_server_error(
            "캐릭터 조회 실패",
            e,
            log_context="Character list failed (dev DB query)",
        )
    except (TypeError, ValueError) as e:
        # `_serialize_character_response_data`의 tags JSON 파싱/직렬화 결과가 비정상이면 여기로 온다.
        _raise_internal_server_error(
            "캐릭터 조회 실패",
            e,
            log_context="Character list failed (dev unexpected)",
        )

@router.post("/characters")
async def create_character(
    character: CharacterCreate,
    db: AsyncSession = Depends(get_db)
):
    """
    [DEVELOPMENT ONLY] 개발 환경용: 인증 없이 캐릭터 생성 (임시 user_id="dev-user" 사용)

    WARNING: 이 엔드포인트는 개발 및 디버깅 목적으로만 사용해야 합니다.
    실제 서비스에서는 보안을 위해 비활성화하거나 제거해야 합니다.
    """
    try:
        db_character = character_service.build_character_model_for_create(
            user_id="dev-user",  # 개발 환경용 임시 사용자 ID
            payload=character,
        )
        
        db.add(db_character)
        await db.commit()
        await db.refresh(db_character)
        
        return {
            "success": True,
            "data": _serialize_character_response_data(db_character)
        }
    except ValueError as e:
        await db.rollback()
        _raise_bad_request("캐릭터 생성 실패", e, log_context="Character create failed (dev validation)")
    except SQLAlchemyError as e:
        await _rollback_and_raise_internal_server_error(
            db=db,
            detail_prefix="캐릭터 생성 실패",
            exc=e,
            log_context="Character create failed (dev DB)",
        )
    except TypeError as e:
        # Pydantic/ORM 객체 직렬화 경계에서 비정형 타입 오류가 날 수 있어 typed pre-catch로 먼저 분리한다.
        # 상태코드/응답 shape는 기존 broad except와 동일하게 500으로 유지한다.
        await _rollback_and_raise_internal_server_error(
            db=db,
            detail_prefix="캐릭터 생성 실패",
            exc=e,
            log_context="Character create failed (dev type mismatch)",
        )
    except Exception as e:
        # broad except 유지 이유: payload 속성 누락/예상치 못한 ORM 상태 오류도 기존 500 shape로 고정해야 한다.
        await _rollback_and_raise_internal_server_error(
            db=db,
            detail_prefix="캐릭터 생성 실패",
            exc=e,
            log_context="Character create failed (/characters dev unexpected)",
        )


# ----- 사용자 엔드포인트 (인증 필요) -----
# [구간 의도]
# - 실서비스 사용자 기준 캐릭터 CRUD 경로.
# - 권한 체크/소유권 규칙은 helper로 공통화하고, 라우터는 transport + 예외 정책 매핑 중심으로 유지한다.
@router.get("/characters/my")
async def list_my_characters(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """내 캐릭터 페르소나 목록 (user_id=me, DB only, is_preset=False)"""
    try:
        result = await db.execute(
            select(Character).where(
                Character.user_id == current_user.id,
                Character.is_preset == False,
            ).order_by(Character.created_at.desc())
        )
        characters = result.scalars().all()
        character_list = [_serialize_character_response_data(char) for char in characters]
        return {"success": True, "data": {"characters": character_list}}
    except SQLAlchemyError as e:
        _raise_internal_server_error(
            "캐릭터 조회 실패",
            e,
            log_context="Character list failed (my DB query)",
        )
    except (TypeError, ValueError) as e:
        # `_serialize_character_response_data`의 tags JSON 파싱/직렬화 결과가 비정상이면 여기로 온다.
        _raise_internal_server_error(
            "캐릭터 조회 실패",
            e,
            log_context="Character list failed (my unexpected)",
        )


@router.post("/characters/my")
async def create_my_character(
    character: CharacterCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """내 캐릭터 생성 (user_id=current_user.id)"""
    try:
        db_character = character_service.build_character_model_for_create(
            user_id=current_user.id,
            payload=character,
        )
        db.add(db_character)
        await db.commit()
        await db.refresh(db_character)
        return {
            "success": True,
            "data": _serialize_character_response_data(db_character)
        }
    except ValueError as e:
        await db.rollback()
        _raise_bad_request("캐릭터 생성 실패", e, log_context="Character create failed (my validation)")
    except SQLAlchemyError as e:
        await _rollback_and_raise_internal_server_error(
            db=db,
            detail_prefix="캐릭터 생성 실패",
            exc=e,
            log_context="Character create failed (my DB)",
        )
    except TypeError as e:
        # create 경로의 잔여 타입 오류(직렬화/ORM 상태)는 broad except 전에 분리해서 로그 맥락을 명확히 남긴다.
        await _rollback_and_raise_internal_server_error(
            db=db,
            detail_prefix="캐릭터 생성 실패",
            exc=e,
            log_context="Character create failed (my type mismatch)",
        )
    except Exception as e:
        # broad except 유지 이유: payload shape/ORM 상태 이상 등 비정형 오류도 기존 500 응답 형식을 유지해야 한다.
        await _rollback_and_raise_internal_server_error(
            db=db,
            detail_prefix="캐릭터 생성 실패",
            exc=e,
            log_context="Character create failed (/characters/my unexpected)",
        )


# ----- 관리자: 캐릭터–Voice 연결/교체 ( /characters/{character_id} 보다 위에 배치 ) -----
# [구간 의도]
# - preset JSON + DB 캐릭터를 모두 다루는 관리자용 유지보수 경로.
# - 경로 충돌 방지를 위해 `/characters/{character_id}` 동적 라우트보다 위에 배치한다 (호환성 중요).

@router.get("/characters/admin/all")
async def list_admin_characters(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """관리자: DB + Preset 캐릭터 통합 목록 (voice 연결/교체용)"""
    try:
        # Preset
        preset_chars = load_preset_characters()
        # DB (전체)
        result = await db.execute(
            select(Character).order_by(Character.created_at.desc())
        )
        db_chars = result.scalars().all()
        merged_payload = character_service.build_admin_character_list_payload(
            preset_characters=preset_chars,
            db_characters=db_chars,
        )
        # [왜 라우터에서 한 번 더 Pydantic 검증하나]
        # 서비스는 순수 payload 생성만 담당하고, 응답 직전 shape 검증은 라우터에서 유지한다.
        merged = [AdminCharacterListItem(**item).model_dump() for item in merged_payload]
        return {"success": True, "data": {"characters": merged}}
    except (FileNotFoundError, TypeError, ValueError) as e:
        _raise_internal_server_error(
            "캐릭터 목록 조회 실패",
            e,
            log_context="Admin character list failed (preset file/json)",
        )
    except SQLAlchemyError as e:
        _raise_internal_server_error(
            "캐릭터 목록 조회 실패",
            e,
            log_context="Admin character list failed (DB query)",
        )


@router.patch("/characters/admin/{character_id}/voice")
async def update_admin_character_voice(
    character_id: str,
    body: AdminCharacterVoiceUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """관리자: 캐릭터 voice_id만 수정 (DB 또는 Preset JSON). null=연결 해제."""
    try:
        # preset lookup은 서비스 helper로 묶어 라우터 분기 가독성을 높인다.
        preset_char = character_service.load_preset_character_by_id_from_disk(character_id)

        if preset_char:
            try:
                payload = character_service.update_preset_character_voice_id(
                    preset_char=preset_char,
                    voice_id=body.voice_id,
                )
            except FileNotFoundError as e:
                detail = str(e)
                if "dir not found" in detail:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="프리셋 디렉터리를 찾을 수 없습니다",
                    )
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"사전설정 캐릭터 파일을 찾을 수 없습니다: {detail.split(':', 1)[-1].strip()}",
                )
            return {"success": True, "data": payload}

        # DB (404 규칙은 공통 helper 재사용)
        character = await _get_db_character_or_404(db=db, character_id=character_id)
        character.voice_id = body.voice_id
        await db.commit()
        await db.refresh(character)
        return {"success": True, "data": {"id": character.id, "name": character.name, "voice_id": character.voice_id}}
    except HTTPException:
        raise
    except ValueError as e:
        await db.rollback()
        _raise_bad_request("voice_id 수정 실패", e, log_context="Admin character voice update failed (validation)")
    except SQLAlchemyError as e:
        await _rollback_and_raise_internal_server_error(
            db=db,
            detail_prefix="voice_id 수정 실패",
            exc=e,
            log_context="Admin character voice update failed (DB)",
        )
    except OSError as e:
        # preset JSON read/write 단계의 파일시스템 오류는 HTTP shape를 바꾸지 않고 기존 500 정책으로 묶는다.
        await _rollback_and_raise_internal_server_error(
            db=db,
            detail_prefix="voice_id 수정 실패",
            exc=e,
            log_context="Admin character voice update failed (preset file I/O)",
        )
    except TypeError as e:
        # preset JSON 구조가 비정상이거나 response payload 조합 시 타입이 깨진 경우를 broad except 전에 분리한다.
        await _rollback_and_raise_internal_server_error(
            db=db,
            detail_prefix="voice_id 수정 실패",
            exc=e,
            log_context="Admin character voice update failed (preset payload type)",
        )
    except Exception as e:
        # broad except 유지 이유: preset 파일 쓰기/DB 상태/기타 예외를 모두 기존 500 shape로 고정하기 위함.
        await _rollback_and_raise_internal_server_error(
            db=db,
            detail_prefix="voice_id 수정 실패",
            exc=e,
            log_context="Admin character voice update failed (/characters/admin/{id}/voice unexpected)",
        )


@router.get("/characters/{character_id}")
async def get_character(
    character_id: str,
    db: AsyncSession = Depends(get_db)
):
    """특정 캐릭터 상세 조회 (인증 없음)"""
    try:
        # 상세 조회는 preset 우선 -> DB fallback 정책을 유지한다.
        preset_char = character_service.load_preset_character_by_id_from_disk(character_id)
        
        if preset_char:
            data = dict(preset_char)
            data["is_preset"] = True
            return {"success": True, "data": data}
        character = await _get_db_character_or_404(db=db, character_id=character_id)
        
        return {
            "success": True,
            "data": _serialize_character_response_data(character)
        }
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        _raise_internal_server_error(
            "캐릭터 조회 실패",
            e,
            log_context="Character detail failed (DB query)",
        )
    except (TypeError, ValueError) as e:
        # DB character 직렬화 시 tags JSON 손상/타입 이상을 기존 500 정책으로 매핑한다.
        _raise_internal_server_error(
            "캐릭터 조회 실패",
            e,
            log_context="Character detail failed (unexpected)",
        )

@router.put("/characters/{character_id}")
async def update_character(
    character_id: str,
    character_update: CharacterUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """캐릭터 수정 (본인 소유 확인)"""
    try:
        preset_chars = load_preset_characters()
        _ensure_not_preset_character(
            preset_chars=preset_chars,
            character_id=character_id,
            error_message="사전설정 캐릭터는 수정할 수 없습니다",
        )
        character = await _get_owned_character_or_raise(
            db=db,
            character_id=character_id,
            current_user=current_user,
        )
        
        update_data = character_update.model_dump(exclude_unset=True)
        if "tags" in update_data and update_data["tags"] is not None:
            update_data["tags"] = character_service.encode_tags_for_storage(update_data["tags"])
        
        for field, value in update_data.items():
            setattr(character, field, value)
        
        await db.commit()
        await db.refresh(character)
        
        return {
            "success": True,
            "data": _serialize_character_response_data(character)
        }
    except HTTPException:
        raise
    except ValueError as e:
        await db.rollback()
        _raise_bad_request("캐릭터 수정 실패", e, log_context="Character update failed (validation)")
    except SQLAlchemyError as e:
        await _rollback_and_raise_internal_server_error(
            db=db,
            detail_prefix="캐릭터 수정 실패",
            exc=e,
            log_context="Character update failed (DB)",
        )
    except (FileNotFoundError, OSError) as e:
        # preset 목록 로드/파일 접근 단계 오류를 broad except 전에 분리한다.
        # 상태코드/응답 shape는 기존과 동일하게 500으로 유지한다.
        await _rollback_and_raise_internal_server_error(
            db=db,
            detail_prefix="캐릭터 수정 실패",
            exc=e,
            log_context="Character update failed (preset file I/O)",
        )
    except TypeError as e:
        # tags 직렬화/응답 직렬화/ORM 필드 대입 경계의 타입 오류를 broad except 전에 분리해 로그 해상도를 높인다.
        await _rollback_and_raise_internal_server_error(
            db=db,
            detail_prefix="캐릭터 수정 실패",
            exc=e,
            log_context="Character update failed (type mismatch)",
        )
    except Exception as e:
        # broad except 유지 이유: update payload/ORM 상태 이상 등 비정형 오류까지 기존 500 응답 형식을 유지해야 한다.
        await _rollback_and_raise_internal_server_error(
            db=db,
            detail_prefix="캐릭터 수정 실패",
            exc=e,
            log_context="Character update failed (/characters/{id} unexpected)",
        )

@router.delete("/characters/{character_id}")
async def delete_character(
    character_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """캐릭터 삭제 (본인 소유 확인)"""
    try:
        preset_chars = load_preset_characters()
        _ensure_not_preset_character(
            preset_chars=preset_chars,
            character_id=character_id,
            error_message="사전설정 캐릭터는 삭제할 수 없습니다",
        )
        character = await _get_owned_character_or_raise(
            db=db,
            character_id=character_id,
            current_user=current_user,
            forbidden_message="캐릭터를 삭제할 권한이 없습니다",
        )
        
        await db.execute(delete(Character).where(Character.id == character_id))
        await db.commit()
        
        return {
            "success": True,
            "data": {
                "message": "캐릭터가 삭제되었습니다"
            }
        }
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        await _rollback_and_raise_internal_server_error(
            db=db,
            detail_prefix="캐릭터 삭제 실패",
            exc=e,
            log_context="Character delete failed (DB)",
        )
    except (FileNotFoundError, OSError, TypeError, ValueError) as e:
        # delete 경로는 preset 목록 로드/JSON 파싱/보조 helper 타입 오류가 섞일 수 있다.
        # 기존 계약을 위해 상태코드는 바꾸지 않고 500 shape만 유지한다.
        await _rollback_and_raise_internal_server_error(
            db=db,
            detail_prefix="캐릭터 삭제 실패",
            exc=e,
            log_context="Character delete failed (preset guard/type)",
        )
    except Exception as e:
        # broad except 유지 이유: delete 경로의 비정형 내부 오류도 기존 500 shape로 고정한다.
        await _rollback_and_raise_internal_server_error(
            db=db,
            detail_prefix="캐릭터 삭제 실패",
            exc=e,
            log_context="Character delete failed (/characters/{id} unexpected)",
        )


# ----- AI 생성 엔드포인트 -----
# [구간 의도]
# - 캐릭터 CRUD와 같은 파일에 있지만, 실제 성격은 LLM 오케스트레이션 엔드포인트다.
# - 1차 리팩토링에서는 path/응답 shape 유지가 우선이라 파일 분리 대신 섹션 주석으로 경계만 명확히 둔다.
@router.post("/generate", response_model=dict)
async def generate_character_details(
    request: GenerateRequest,
    current_user: User = Depends(get_current_user)
):
    """
    AI를 사용하여 캐릭터의 상세 정보(페르소나, 말투 등)를 생성합니다.
    순서: Gemini -> Local LLM (glm-4.7-flash) -> Mock Data
    """
    # 1. Mock Data 준비 (최후의 수단)
    mock_data = {
        "persona": f"성격: {request.name}은 매우 성실하고 계획적입니다.\n말투: 정중하고 차분한 말투를 사용합니다.\n배경: 평범한 학생이었으나 사건을 겪으며 변화했습니다.\n목표: 진실을 밝히는 것이 최종 목표입니다.",
        "description": f"{request.name} 캐릭터의 상세 설정입니다.",
        "category": request.category or "일반",
        "tags": ["성실", "정중", "목표의식"]
    }

    prompt = f"{request.name} 캐릭터에 대한 상세한 정보를 JSON 형식으로 생성해주세요. 배경설명: {request.description}\n카테고리: {request.category}\n\n" \
             f"다음 필드들을 포함해야 합니다:\n" \
             f"- persona: 캐릭터의 성격, 말투, 행동 패턴을 상세히 설명 (200자 이상). 다음 형식 포함: 성격, 말투, 배경, 목표\n" \
             f"- description: 캐릭터 요약 설명\n" \
             f"- tags: 관련 태그 3-5개 배열\n\n" \
             f"JSON 형식으로만 응답하세요."

    messages = [{"role": "user", "content": prompt}]

    # JSON 파싱 헬퍼
    def parse_json_response(text: str) -> Dict[str, Any]:
        # [왜 내부 helper를 유지하나]
        # 이 엔드포인트의 문서/분석 기준에서 nested helper 자체가 구조 포인트라서 이름은 유지.
        # 실제 파싱 규칙은 공통 service로 위임해서 중복만 제거한다.
        return character_service.extract_json_object_from_llm_text(text)

    # 1. Try Gemini -> Local LLM (Fallback) 공통 흐름
    try:
        content, _is_fallback = await character_service.call_llm_json_with_gemini_fallback(
            messages=messages,
            system_instruction=None,
            fallback_model="glm-4.7-flash",
            temperature=0.7,
            max_tokens=2000,
            logger_hint="Character generate (/api/characters/generate)",
        )
        data = parse_json_response(content)
        character_service.normalize_string_list_fields(data, ["tags"])
        return {"success": True, "data": data}
    except Exception as e:
        # broad except 유지 이유: 이 엔드포인트는 "Mock fallback"이 계약의 일부라서
        # LLM/파싱/외부 의존성 실패를 모두 한 곳에서 흡수해야 기존 UX가 유지된다.
        # 로그 문맥은 `/generate` 경로를 포함해 운영 로그 grep 시 CRUD 실패 로그와 구분되게 유지한다.
        logger.warning("Character generate fallback chain failed, using mock data: %s", e)

    # 3. Fallback to Mock
    return {"success": True, "data": mock_data}
