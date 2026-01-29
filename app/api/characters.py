from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import json
import os
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
import httpx

from app.core.config import settings
from app.api.deps import get_db, get_current_user, require_admin
from app.models.user import User
from app.models.character import Character

router = APIRouter()

# 사전설정 캐릭터 캐시: (base, mtime) 변경 시 무효화
_preset_characters_cache: Optional[List[Dict[str, Any]]] = None
_cache_key: Optional[tuple] = None  # (base_path, dir_mtime)


def _resolve_preset_characters_dir() -> Optional[Path]:
    """
    프리셋 캐릭터 디렉터리 해석.
    1) PRESET_CHARACTERS_DIR env
    2) 모노레포 기본: [프로젝트 루트]/madcamp-Screening-Humanity-FRONT/public/characters (exists 시)
    3) 폴백: app/config/characters
    """
    # 1) 환경변수
    env_path = os.environ.get("PRESET_CHARACTERS_DIR")
    if env_path:
        p = Path(env_path)
        if p.is_dir():
            return p
        return None
    # 2) 모노레포 기본: app/api 기준 4단계 상위
    try:
        base = Path(__file__).resolve().parent.parent.parent.parent
        monorepo = base / "madcamp-Screening-Humanity-FRONT" / "public" / "characters"
        if monorepo.is_dir():
            return monorepo
    except Exception:
        pass
    # 3) 폴백: app/config/characters
    fallback = Path(__file__).resolve().parent.parent / "config" / "characters"
    if fallback.is_dir():
        return fallback
    return None


def load_preset_characters() -> List[Dict[str, Any]]:
    """사전설정 캐릭터: public/characters/*.json 폴더 스캔. (base, mtime) 캐시로 디렉터리 변경 시 재스캔."""
    global _preset_characters_cache, _cache_key

    base = _resolve_preset_characters_dir()
    if base is None or not base.is_dir():
        _preset_characters_cache = []
        _cache_key = None
        return []

    try:
        current_mtime = base.stat().st_mtime
        new_key = (str(base), current_mtime)
        if _preset_characters_cache is not None and _cache_key == new_key:
            return _preset_characters_cache

        out: List[Dict[str, Any]] = []
        for f in sorted(base.glob("*.json")):
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    obj = json.load(fp)
                if not isinstance(obj, dict):
                    continue
                
                # [FIX] JSON 내부 ID가 파일명과 다르면 DB 연결이 깨짐.
                # 강제로 파일명(stem)을 ID로 사용.
                obj["id"] = f.stem
                
                # 파일명(확장자 포함)을 메타데이터로 저장 (ID 불일치 대비 - 이제는 ID=Filename이므로 덜 중요하지만 유지)
                obj["_filename"] = f.name
                out.append(obj)
            except Exception as e:
                print(f"사전설정 JSON 로드 실패 {f}: {e}")
                continue
        _preset_characters_cache = out
        _cache_key = new_key
        return out
    except Exception as e:
        print(f"사전설정 캐릭터 로드 실패: {e}")
        _preset_characters_cache = []
        _cache_key = None
        return []

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


# Endpoints
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
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"사전설정 캐릭터 조회 실패: {str(e)}"
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
        
        character_list = []
        for char in characters:
            tags = json.loads(char.tags) if char.tags else []
            character_list.append({
                "id": char.id,
                "name": char.name,
                "description": char.description,
                "persona": char.persona,
                "voice_id": char.voice_id,
                "category": char.category,
                "tags": tags,
                "image_url": char.image_url,
                "is_preset": char.is_preset,
                "user_id": char.user_id,
                "created_at": char.created_at.isoformat() if char.created_at else None
            })
        
        return {
            "success": True,
            "data": {
                "characters": character_list
            }
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"캐릭터 조회 실패: {str(e)}"
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
        tags_json = json.dumps(character.tags) if character.tags else None
        
        db_character = Character(
            user_id="dev-user",  # 개발 환경용 임시 사용자 ID
            name=character.name,
            description=character.description,
            persona=character.persona,
            voice_id=character.voice_id,
            category=character.category,
            tags=tags_json,
            image_url=character.image_url,
            is_preset=False
        )
        
        db.add(db_character)
        await db.commit()
        await db.refresh(db_character)
        
        tags = json.loads(db_character.tags) if db_character.tags else []
        return {
            "success": True,
            "data": {
                "id": db_character.id,
                "name": db_character.name,
                "description": db_character.description,
                "persona": db_character.persona,
                "voice_id": db_character.voice_id,
                "category": db_character.category,
                "tags": tags,
                "image_url": db_character.image_url,
                "is_preset": db_character.is_preset,
                "user_id": db_character.user_id,
                "created_at": db_character.created_at.isoformat() if db_character.created_at else None
            }
        }
    except Exception as e:
        await db.rollback()
        print(f"Character create error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"캐릭터 생성 실패: {str(e)}"
        )


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
        character_list = []
        for char in characters:
            tags = json.loads(char.tags) if char.tags else []
            character_list.append({
                "id": char.id,
                "name": char.name,
                "description": char.description,
                "persona": char.persona,
                "voice_id": char.voice_id,
                "category": char.category,
                "tags": tags,
                "image_url": char.image_url,
                "is_preset": char.is_preset,
                "user_id": char.user_id,
                "created_at": char.created_at.isoformat() if char.created_at else None
            })
        return {"success": True, "data": {"characters": character_list}}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"캐릭터 조회 실패: {str(e)}"
        )


@router.post("/characters/my")
async def create_my_character(
    character: CharacterCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """내 캐릭터 생성 (user_id=current_user.id)"""
    try:
        tags_json = json.dumps(character.tags) if character.tags else None
        db_character = Character(
            user_id=current_user.id,
            name=character.name,
            description=character.description,
            persona=character.persona,
            voice_id=character.voice_id,
            category=character.category,
            tags=tags_json,
            image_url=character.image_url,
            is_preset=False
        )
        db.add(db_character)
        await db.commit()
        await db.refresh(db_character)
        tags = json.loads(db_character.tags) if db_character.tags else []
        return {
            "success": True,
            "data": {
                "id": db_character.id,
                "name": db_character.name,
                "description": db_character.description,
                "persona": db_character.persona,
                "voice_id": db_character.voice_id,
                "category": db_character.category,
                "tags": tags,
                "image_url": db_character.image_url,
                "is_preset": db_character.is_preset,
                "user_id": db_character.user_id,
                "created_at": db_character.created_at.isoformat() if db_character.created_at else None
            }
        }
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"캐릭터 생성 실패: {str(e)}"
        )


# ----- 관리자: 캐릭터–Voice 연결/교체 ( /characters/{character_id} 보다 위에 배치 ) -----

@router.get("/characters/admin/all")
async def list_admin_characters(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """관리자: DB + Preset 캐릭터 통합 목록 (voice 연결/교체용)"""
    try:
        # Preset
        preset_chars = load_preset_characters()
        preset_list = [
            AdminCharacterListItem(
                id=c.get("id", ""),
                name=c.get("name", ""),
                voice_id=c.get("voice_id"),
                is_preset=True,
                user_id=None,
            )
            for c in preset_chars if c.get("id")
        ]
        # DB (전체)
        result = await db.execute(
            select(Character).order_by(Character.created_at.desc())
        )
        db_chars = result.scalars().all()
        db_list = [
            AdminCharacterListItem(
                id=char.id,
                name=char.name,
                voice_id=char.voice_id,
                is_preset=bool(char.is_preset),
                user_id=char.user_id,
            )
            for char in db_chars
        ]
        merged = preset_list + db_list
        return {"success": True, "data": {"characters": [c.model_dump() for c in merged]}}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"캐릭터 목록 조회 실패: {str(e)}"
        )


@router.patch("/characters/admin/{character_id}/voice")
async def update_admin_character_voice(
    character_id: str,
    body: AdminCharacterVoiceUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """관리자: 캐릭터 voice_id만 수정 (DB 또는 Preset JSON). null=연결 해제."""
    global _preset_characters_cache, _cache_key
    try:
        preset_chars = load_preset_characters()
        preset_char = next((c for c in preset_chars if c.get("id") == character_id), None)

        if preset_char:
            # Preset: {id}.json 단일 파일 읽기·쓰기
            base = _resolve_preset_characters_dir()
            if not base or not base.is_dir():
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="프리셋 디렉터리를 찾을 수 없습니다")
            
            # ID와 파일명이 다를 수 있으므로 _filename 메타데이터 활용
            filename = preset_char.get("_filename")
            if not filename:
                # Fallback: ID를 파일명으로 가정 (구버전 호환)
                filename = f"{character_id}.json"
            
            path = base / filename
            if not path.exists():
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"사전설정 캐릭터 파일을 찾을 수 없습니다: {filename}")

            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            obj["voice_id"] = body.voice_id if body.voice_id is not None else None
            with open(path, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
            _preset_characters_cache = None
            _cache_key = None
            return {"success": True, "data": {"id": character_id, "name": preset_char.get("name", ""), "voice_id": body.voice_id}}

        # DB
        result = await db.execute(select(Character).where(Character.id == character_id))
        character = result.scalar_one_or_none()
        if not character:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="캐릭터를 찾을 수 없습니다")
        character.voice_id = body.voice_id
        await db.commit()
        await db.refresh(character)
        return {"success": True, "data": {"id": character.id, "name": character.name, "voice_id": character.voice_id}}
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"voice_id 수정 실패: {str(e)}"
        )


@router.get("/characters/{character_id}")
async def get_character(
    character_id: str,
    db: AsyncSession = Depends(get_db)
):
    """특정 캐릭터 상세 조회 (인증 없음)"""
    try:
        preset_chars = load_preset_characters()
        preset_char = next((c for c in preset_chars if c.get("id") == character_id), None)
        
        if preset_char:
            data = dict(preset_char)
            data["is_preset"] = True
            return {"success": True, "data": data}
        
        result = await db.execute(
            select(Character).where(Character.id == character_id)
        )
        character = result.scalar_one_or_none()
        
        if not character:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="캐릭터를 찾을 수 없습니다"
            )
        
        tags = json.loads(character.tags) if character.tags else []
        return {
            "success": True,
            "data": {
                "id": character.id,
                "name": character.name,
                "description": character.description,
                "persona": character.persona,
                "voice_id": character.voice_id,
                "category": character.category,
                "tags": tags,
                "image_url": character.image_url,
                "is_preset": character.is_preset,
                "user_id": character.user_id,
                "created_at": character.created_at.isoformat() if character.created_at else None
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"캐릭터 조회 실패: {str(e)}"
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
        preset_char = next((c for c in preset_chars if c.get("id") == character_id), None)
        if preset_char:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="사전설정 캐릭터는 수정할 수 없습니다"
            )
        
        result = await db.execute(
            select(Character).where(Character.id == character_id)
        )
        character = result.scalar_one_or_none()
        
        if not character:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="캐릭터를 찾을 수 없습니다"
            )
            
        if character.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="캐릭터를 수정할 권한이 없습니다"
            )
        
        update_data = character_update.model_dump(exclude_unset=True)
        if "tags" in update_data and update_data["tags"] is not None:
            update_data["tags"] = json.dumps(update_data["tags"])
        
        for field, value in update_data.items():
            setattr(character, field, value)
        
        await db.commit()
        await db.refresh(character)
        
        tags = json.loads(character.tags) if character.tags else []
        return {
            "success": True,
            "data": {
                "id": character.id,
                "name": character.name,
                "description": character.description,
                "persona": character.persona,
                "voice_id": character.voice_id,
                "category": character.category,
                "tags": tags,
                "image_url": character.image_url,
                "is_preset": character.is_preset,
                "user_id": character.user_id,
                "created_at": character.created_at.isoformat() if character.created_at else None
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"캐릭터 수정 실패: {str(e)}"
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
        preset_char = next((c for c in preset_chars if c.get("id") == character_id), None)
        if preset_char:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="사전설정 캐릭터는 삭제할 수 없습니다"
            )
        
        result = await db.execute(
            select(Character).where(Character.id == character_id)
        )
        character = result.scalar_one_or_none()
        
        if not character:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="캐릭터를 찾을 수 없습니다"
            )
            
        if character.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="캐릭터를 삭제할 권한이 없습니다"
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
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"캐릭터 삭제 실패: {str(e)}"
        )

@router.post("/generate", response_model=dict)
async def generate_character_details(
    request: GenerateRequest,
    current_user: User = Depends(get_current_user)
):
    """
    AI를 사용하여 캐릭터의 상세 정보(페르소나, 말투 등)를 생성합니다.
    순서: Gemini -> Local LLM (glm-4.7-flash) -> Mock Data
    """
    from app.core.llm import call_llm  # 지연 import로 순환 참조 방지

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
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        return json.loads(text)

    # 1. Try Gemini
    if settings.GEMINI_API_KEY:
        try:
            # Gemini-2.0-flash 호출
            result = await call_llm(messages, model="gemini-2.0-flash", json_mode=True)
            data = parse_json_response(result["content"])
            return {"success": True, "data": data}
        except Exception as e:
            print(f"Gemini generation failed: {e}. Trying local LLM...")
    
    # 2. Try Local LLM (Fallback)
    try:
        # 로컬 모델 (glm-4.7-flash) 호출
        # json_mode=True는 모델이 지원해야 함. 지원 안 하면 텍스트 파싱 시도.
        result = await call_llm(messages, model="glm-4.7-flash", json_mode=True)
        data = parse_json_response(result["content"])
        return {"success": True, "data": data}
    except Exception as e:
        print(f"Local LLM generation failed: {e}. Using mock data.")

    # 3. Fallback to Mock
    return {"success": True, "data": mock_data}
