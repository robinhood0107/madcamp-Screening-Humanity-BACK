"""
캐릭터 생성/파싱 관련 공통 서비스.

[의도]
- `app/api/ai.py`, `app/api/characters.py`에 중복된 JSON 파싱/목록 정규화/Gemini→로컬 LLM fallback 패턴을 모은다.
- 1차는 보수적으로 "공통 로직만" 추출하고, 엔드포인트별 응답 shape/에러 정책은 라우터에 남긴다.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.llm import call_llm
from app.models.character import Character

logger = logging.getLogger(__name__)

# 프리셋 캐릭터 캐시: (base_path, dir_mtime) 기준으로 무효화
_preset_characters_cache: Optional[List[Dict[str, Any]]] = None
_preset_cache_key: Optional[Tuple[str, float]] = None


def resolve_preset_characters_dir() -> Optional[Path]:
    """
    [역할]
    프리셋 캐릭터 JSON 디렉터리를 현재 실행 환경 기준으로 해석한다.

    [왜 여기서 처리하나]
    - `characters.py` 라우터 내부에 있던 파일시스템 탐색 규칙을 서비스로 이동해 재사용/테스트 가능하게 만든다.
    - `chat.py` 등 다른 모듈도 프리셋 정보를 읽기 때문에 경로 해석 규칙이 한 곳에 있어야 유지보수가 쉽다.

    [입력/출력]
    - 입력: 없음 (환경변수/현재 파일 위치 사용)
    - 출력: 프리셋 디렉터리 `Path` 또는 `None`

    [부작용]
    - 환경변수 조회, 파일시스템 존재 확인

    [실패/예외]
    - 내부 경로 계산 실패 시 조용히 다음 후보를 시도하고, 최종적으로 `None` 반환

    [주의]
    - 우선순위는 기존 호환성 유지: `PRESET_CHARACTERS_DIR` → monorepo FRONT/public/characters → app/config/characters
    """
    env_path = os.environ.get("PRESET_CHARACTERS_DIR")
    if env_path:
        candidate = Path(env_path)
        return candidate if candidate.is_dir() else None

    try:
        # services/character_service.py -> app/services -> app -> BACK -> monorepo root
        base = Path(__file__).resolve().parent.parent.parent.parent
        monorepo = base / "madcamp-Screening-Humanity-FRONT" / "public" / "characters"
        if monorepo.is_dir():
            return monorepo
    except Exception:
        pass

    fallback = Path(__file__).resolve().parent.parent / "config" / "characters"
    if fallback.is_dir():
        return fallback
    return None


def invalidate_preset_character_cache() -> None:
    """
    [역할]
    프리셋 캐릭터 디렉터리 스캔 결과 캐시를 강제로 무효화한다.

    [왜 여기서 처리하나]
    - Preset JSON 수정(관리자 voice_id 변경) 이후 즉시 재조회 시 stale cache가 보이면 운영자가 헷갈린다.
    - 캐시 상태를 서비스 모듈 내부에 두고 무효화 API만 노출하는 편이 라우터가 단순해진다.

    [입력/출력]
    - 입력/출력 없음

    [부작용]
    - 모듈 전역 캐시 상태 초기화

    [실패/예외]
    - 예외 없음

    [주의]
    - 캐시 정책을 바꾸더라도 이 함수 시그니처는 유지하는 것이 라우터 호환성에 유리하다.
    """
    global _preset_characters_cache, _preset_cache_key
    _preset_characters_cache = None
    _preset_cache_key = None


def load_preset_characters_from_disk() -> List[Dict[str, Any]]:
    """
    [역할]
    프리셋 캐릭터 JSON 파일들을 읽어 목록으로 반환한다. (디렉터리 mtime 기반 캐시 포함)

    [왜 여기서 처리하나]
    - `characters.py`의 파일 I/O + 캐시 + JSON 정규화 로직을 서비스 계층으로 모아 라우터를 얇게 만든다.
    - 향후 프리셋 스키마 검증/마이그레이션을 붙일 때 한 지점에서 처리하기 쉽다.

    [입력/출력]
    - 입력: 없음
    - 출력: 프리셋 캐릭터 dict 목록 (각 항목은 `id`, `_filename` 메타 포함)

    [부작용]
    - 파일시스템 읽기, 모듈 캐시 갱신, 로깅

    [실패/예외]
    - 일부 JSON 파일이 깨졌으면 해당 파일만 건너뛴다.
    - 전체 디렉터리 스캔 실패 시 빈 리스트 반환(기존 동작 호환)

    [주의]
    - JSON 내부 `id`는 강제로 파일명(stem)으로 덮어쓴다. (DB/프론트 연결 안정성 계약)
    """
    global _preset_characters_cache, _preset_cache_key

    base = resolve_preset_characters_dir()
    if base is None or not base.is_dir():
        invalidate_preset_character_cache()
        return []

    try:
        current_mtime = base.stat().st_mtime
        new_key = (str(base), current_mtime)
        if _preset_characters_cache is not None and _preset_cache_key == new_key:
            return _preset_characters_cache

        out: List[Dict[str, Any]] = []
        for file_path in sorted(base.glob("*.json")):
            try:
                with open(file_path, "r", encoding="utf-8") as fp:
                    obj = json.load(fp)
                if not isinstance(obj, dict):
                    continue

                # [FIX] JSON 내부 ID와 파일명이 다르면 프론트/DB 연결 키가 흔들린다.
                # 파일명을 단일 진실 원천으로 강제한다.
                obj["id"] = file_path.stem
                obj["_filename"] = file_path.name
                out.append(obj)
            except Exception as e:
                logger.warning("사전설정 JSON 로드 실패 %s: %s", file_path, e)
                continue

        _preset_characters_cache = out
        _preset_cache_key = new_key
        return out
    except Exception as e:
        logger.warning("사전설정 캐릭터 로드 실패: %s", e)
        invalidate_preset_character_cache()
        return []


def update_preset_character_voice_id(
    *,
    preset_char: Dict[str, Any],
    voice_id: Optional[str],
) -> Dict[str, Any]:
    """
    [역할]
    프리셋 캐릭터 JSON 파일의 `voice_id`를 수정하고 캐시를 무효화한다.

    [왜 여기서 처리하나]
    - `characters.py` 관리자 엔드포인트에 파일 경로 해석/JSON read-write/cached invalidate가 한 덩어리로 들어 있어 라우터 책임이 과하다.
    - 프리셋 파일 수정 규칙을 한 곳에 두면 추후 필드 확장(예: image_url, tags)에도 재사용할 수 있다.

    [입력/출력]
    - 입력: 프리셋 캐릭터 dict(`load_preset_characters_from_disk` 결과), 새 voice_id
    - 출력: 최소 응답용 요약 dict (`id`, `name`, `voice_id`)

    [부작용]
    - 파일시스템 read/write
    - 모듈 캐시 무효화

    [실패/예외]
    - 디렉터리/파일을 찾지 못하면 `FileNotFoundError`
    - 파일 포맷 문제/쓰기 실패는 예외를 그대로 올린다 (라우터에서 HTTPException 정책 적용)

    [주의]
    - 파일명은 `_filename` 메타를 우선 사용한다. 없으면 `{id}.json` fallback 유지(구버전 호환).
    """
    base = resolve_preset_characters_dir()
    if not base or not base.is_dir():
        raise FileNotFoundError("preset characters dir not found")

    filename = preset_char.get("_filename") or f"{preset_char.get('id')}.json"
    path = base / filename
    if not path.exists():
        raise FileNotFoundError(f"preset character file not found: {filename}")

    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    obj["voice_id"] = voice_id if voice_id is not None else None
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

    invalidate_preset_character_cache()
    return {
        "id": preset_char.get("id", ""),
        "name": preset_char.get("name", ""),
        "voice_id": voice_id,
    }


def find_preset_character_by_id(
    preset_characters: List[Dict[str, Any]],
    character_id: str,
) -> Optional[Dict[str, Any]]:
    """
    [역할]
    프리셋 캐릭터 목록에서 ID로 단일 캐릭터를 찾는다.

    [왜 여기서 처리하나]
    - `characters.py`, `chat.py`에서 같은 `next(... if c.get('id') == character_id)` 패턴이 반복된다.
    - 조회 규칙을 한 곳에 두면 `_filename`/id 정책이 바뀔 때 수정 지점이 줄어든다.

    [입력/출력]
    - 입력: 프리셋 캐릭터 dict 목록, 찾을 ID
    - 출력: 프리셋 캐릭터 dict 또는 `None`

    [부작용]
    - 없음

    [실패/예외]
    - 예외 없음

    [주의]
    - 목록은 `load_preset_characters_from_disk()` 결과를 전제로 한다(`id`가 파일명으로 강제되어 있음).
    """
    return next((c for c in preset_characters if c.get("id") == character_id), None)


def load_preset_character_by_id_from_disk(character_id: str) -> Optional[Dict[str, Any]]:
    """
    [역할]
    프리셋 캐릭터를 디스크(캐시 포함)에서 읽은 목록 기준으로 ID 조회한다.

    [왜 여기서 처리하나]
    - `characters.py`에서 `load_preset_characters()` + `find_preset_character_by_id(...)` 조합이 반복된다.
    - 목록 로드 정책(캐시/파일 스캔)과 ID 조회 정책을 서비스 계층에서 같이 재사용하면 라우터 분기 가독성이 좋아진다.

    [입력/출력]
    - 입력: 캐릭터 ID
    - 출력: 프리셋 캐릭터 dict 또는 `None`

    [부작용]
    - preset 캐시 로더 호출(필요 시 파일 I/O)

    [실패/예외]
    - `load_preset_characters_from_disk()`의 실패 정책을 그대로 따른다(기본적으로 빈 리스트/경고 로그)

    [주의]
    - 1차 리팩토링에서는 "조회"만 공통화한다. 파일 수정/에러 응답 매핑은 라우터 책임 유지.
    """
    return find_preset_character_by_id(load_preset_characters_from_disk(), character_id)


async def resolve_character_voice_id_for_tts(
    *,
    character_id: Optional[str],
    db: AsyncSession,
    current_user_id: Optional[str],
    default_voice_id: str = "default",
) -> str:
    """
    [역할]
    채팅 TTS 합성용 `voice_id`를 캐릭터 ID 기준으로 해석한다. (DB 우선, 프리셋 fallback)

    [왜 여기서 처리하나]
    - `chat.py`에 남아 있던 DB 조회 + preset 조회 + 권한 체크 + default fallback 블록을 서비스로 모아 라우터를 얇게 만든다.
    - voice_id 해석 정책은 향후 `/chat`, `/chat/stream`, 별도 TTS 오케스트레이션에서 재사용될 가능성이 높다.

    [입력/출력]
    - 입력: character_id, DB 세션, 현재 사용자 ID(선택), 기본 voice_id
    - 출력: 합성에 사용할 최종 voice_id 문자열

    [부작용]
    - DB 조회 1회 가능
    - preset 캐시 로더 호출(파일 읽기 포함 가능)

    [실패/예외]
    - DB 오류 등은 상위 라우터로 전파한다 (API 에러 정책은 라우터 책임)

    [주의]
    - 기존 호환성 유지:
      1) DB 캐릭터가 있고 접근 가능하면 DB voice_id 사용
      2) DB 캐릭터가 없으면 preset lookup
      3) 그 외는 default 유지
    """
    if not character_id:
        return default_voice_id

    result = await db.execute(select(Character).where(Character.id == character_id))
    character = result.scalar_one_or_none()
    if character:
        can_use = character.is_preset or (current_user_id and character.user_id == current_user_id)
        if can_use:
            return character.voice_id or default_voice_id
        return default_voice_id

    preset = find_preset_character_by_id(load_preset_characters_from_disk(), character_id)
    if preset:
        return preset.get("voice_id") or default_voice_id
    return default_voice_id


def encode_tags_for_storage(tags: Optional[Iterable[str]]) -> Optional[str]:
    """
    [역할]
    캐릭터 태그 리스트를 DB 저장용 JSON 문자열로 변환한다.

    [왜 여기서 처리하나]
    - `characters.py`의 create/update 경로에서 동일한 `json.dumps` 규칙이 반복된다.
    - None 처리 정책을 한 곳에 고정하면 라우터별 미세한 차이로 인한 회귀를 줄일 수 있다.

    [입력/출력]
    - 입력: 태그 iterable 또는 None
    - 출력: JSON 문자열 또는 None

    [부작용]
    - 없음

    [실패/예외]
    - 직렬화 불가능한 원소가 있으면 `TypeError`/`ValueError`가 발생할 수 있다 (상위 라우터에서 처리).

    [주의]
    - 빈 리스트는 기존 동작과 호환되게 `None`으로 저장한다.
    """
    if not tags:
        return None
    return json.dumps(list(tags))


def serialize_character_model(character: Character) -> Dict[str, Any]:
    """
    [역할]
    SQLAlchemy `Character` 모델 인스턴스를 API 응답용 dict로 직렬화한다.

    [왜 여기서 처리하나]
    - `characters.py`에 동일한 필드 매핑/`tags` 파싱/`created_at.isoformat()` 코드가 여러 번 반복된다.
    - 응답 필드 계약을 한 곳에 모아두면 라우터를 얇게 만들면서도 shape를 안정적으로 유지할 수 있다.

    [입력/출력]
    - 입력: `Character` ORM 객체
    - 출력: 기존 라우터 응답과 동일한 키를 가진 dict

    [부작용]
    - 없음 (문자열/필드 변환)

    [실패/예외]
    - `character.tags`가 손상된 JSON이면 `json.JSONDecodeError` 발생 가능 (기존 동작과 동일하게 상위 라우터로 전파)

    [주의]
    - 응답 키/필드명은 프론트 계약이라 1차 리팩토링에서 변경 금지
    """
    tags = json.loads(character.tags) if character.tags else []
    return {
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
        "created_at": character.created_at.isoformat() if character.created_at else None,
    }


def build_character_model_for_create(
    *,
    user_id: str,
    payload: Any,
) -> Character:
    """
    [역할]
    `CharacterCreate` 계열 입력(payload)로부터 DB 저장용 `Character` ORM 객체를 생성한다.

    [왜 여기서 처리하나]
    - `characters.py`의 `create_character`, `create_my_character`가 같은 필드 매핑 코드를 거의 그대로 반복하고 있다.
    - 생성 필드 매핑 규칙(user_id 제외)을 한 곳에 두면 필드 추가/삭제 시 누락 위험이 줄어든다.

    [입력/출력]
    - 입력: 소유자 user_id, `CharacterCreate`와 호환되는 payload 객체(속성 접근)
    - 출력: 아직 DB에 add되지 않은 `Character` ORM 객체

    [부작용]
    - 없음 (ORM 객체 생성만 수행)

    [실패/예외]
    - payload가 필요한 속성을 가지지 않으면 `AttributeError` 가능
    - tags 직렬화 실패 시 `TypeError`/`ValueError`가 전파될 수 있음

    [주의]
    - `is_preset=False` 기본 정책은 기존 create 엔드포인트 호환성 때문에 고정한다.
    """
    tags_json = encode_tags_for_storage(getattr(payload, "tags", None))
    return Character(
        user_id=user_id,
        name=getattr(payload, "name"),
        description=getattr(payload, "description", None),
        persona=getattr(payload, "persona", None),
        voice_id=getattr(payload, "voice_id", None),
        category=getattr(payload, "category", None),
        tags=tags_json,
        image_url=getattr(payload, "image_url", None),
        is_preset=False,
    )


def build_admin_character_list_payload(
    *,
    preset_characters: List[Dict[str, Any]],
    db_characters: List[Character],
) -> List[Dict[str, Any]]:
    """
    [역할]
    관리자 캐릭터 통합 목록 응답용 payload(DB + preset)를 생성한다.

    [왜 여기서 처리하나]
    - `characters.py::list_admin_characters`에 preset/db 각각의 직렬화 list-comprehension이 있어 라우터 본문이 길어진다.
    - 통합 목록 shape를 서비스에서 생성하면 필드 정책(`is_preset`, `user_id`, `voice_id`)을 한 곳에서 유지할 수 있다.

    [입력/출력]
    - 입력: preset 캐릭터 dict 목록, DB `Character` 목록
    - 출력: API 응답용 dict 목록 (`id`, `name`, `voice_id`, `is_preset`, `user_id`)

    [부작용]
    - 없음 (새 리스트 생성)

    [실패/예외]
    - preset/db 항목 shape가 예상과 다르면 KeyError/AttributeError 가능 (호출자 계약 위반)

    [주의]
    - 프론트 관리자 화면 계약 때문에 응답 키 이름은 1차 리팩토링에서 변경 금지.
    """
    preset_list = [
        {
            "id": c.get("id", ""),
            "name": c.get("name", ""),
            "voice_id": c.get("voice_id"),
            "is_preset": True,
            "user_id": None,
        }
        for c in preset_characters
        if c.get("id")
    ]

    db_list = [
        {
            "id": char.id,
            "name": char.name,
            "voice_id": char.voice_id,
            "is_preset": bool(char.is_preset),
            "user_id": char.user_id,
        }
        for char in db_characters
    ]
    return preset_list + db_list


def extract_json_object_from_llm_text(content: str) -> Dict[str, Any]:
    """
    [역할]
    LLM 응답 문자열에서 JSON 객체를 추출/파싱한다.

    [왜 여기서 처리하나]
    - 여러 엔드포인트가 코드펜스 제거 + 중괄호 범위 추출 + json.loads 재시도를 반복하고 있다.
    - 이 파싱 규칙을 한 곳에 두면 포맷 흔들림 대응을 일관되게 유지할 수 있다.

    [입력/출력]
    - 입력: LLM raw 응답 문자열
    - 출력: 파싱된 dict

    [부작용]
    - 없음 (순수 문자열 가공)

    [실패/예외]
    - JSON 추출/파싱 실패 시 `ValueError`를 발생시킨다.

    [주의]
    - 1차는 dict만 지원한다. 배열/스키마 검증까지는 하지 않는다(라우터/상위 로직이 책임).
    """
    content = (content or "").strip()

    # 1) 마크다운 코드 블록 제거
    if "```json" in content:
        content = content.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in content:
        content = content.split("```", 1)[1].split("```", 1)[0].strip()

    # 2) 전체 문자열이 JSON이면 바로 파싱
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # 3) 설명 텍스트가 섞였을 때 중괄호 영역만 잘라서 재시도
    try:
        start_idx = content.find("{")
        end_idx = content.rfind("}")
        if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
            parsed = json.loads(content[start_idx : end_idx + 1])
            if isinstance(parsed, dict):
                return parsed
    except json.JSONDecodeError:
        pass

    raise ValueError("Failed to extract JSON object from LLM response")


def normalize_string_list_fields(payload: Dict[str, Any], fields: Iterable[str]) -> Dict[str, Any]:
    """
    [역할]
    LLM이 배열 대신 문자열로 보낸 필드를 list[str]로 보정한다.

    [왜 여기서 처리하나]
    - Ollama/Gemini 응답 포맷이 흔들릴 때 `likes`, `dislikes`, `tags`가 문자열로 오는 경우가 있다.
    - 라우터마다 같은 보정 코드를 반복하지 않기 위해 공통화한다.

    [입력/출력]
    - 입력: 응답 dict + 보정 대상 필드명 목록
    - 출력: 같은 dict 객체(제자리 수정 후 반환)

    [부작용]
    - 입력 dict를 in-place로 수정한다.

    [실패/예외]
    - 특이 예외 없음. 문자열 외 타입은 그대로 둔다.

    [주의]
    - 콤마 구분 규칙만 적용한다. 더 복잡한 파싱 규칙은 2차에서 검토.
    """
    for field in fields:
        value = payload.get(field)
        if isinstance(value, str):
            payload[field] = [x.strip() for x in value.split(",") if x.strip()]
    return payload


async def call_llm_json_with_gemini_fallback(
    *,
    messages: List[Dict[str, str]],
    system_instruction: Optional[str],
    fallback_model: str,
    temperature: float,
    max_tokens: int,
    logger_hint: str,
) -> Tuple[str, bool]:
    """
    [역할]
    JSON 모드 LLM 호출을 Gemini 우선 → 로컬 LLM 폴백 순서로 수행하고 응답 text를 반환한다.

    [왜 여기서 처리하나]
    - `ai.py`와 `characters.py`에서 같은 fallback 패턴(키 확인/경고 로그/로컬 모델 전환)을 반복하고 있다.
    - 모델명/프롬프트는 엔드포인트별로 다르지만, fallback 흐름 자체는 공통이다.

    [입력/출력]
    - 입력: 메시지/시스템프롬프트/로컬 폴백 모델/샘플링 파라미터
    - 출력: `(content, is_fallback)` 튜플

    [부작용]
    - 외부 LLM HTTP 호출, 로깅

    [실패/예외]
    - Gemini/로컬 모두 실패하면 `RuntimeError` 발생 (라우터가 정책에 맞게 처리)

    [주의]
    - 1차는 응답 파싱/스키마 검증까지 하지 않는다. raw text만 반환하고 파싱은 호출자에서 수행.
    """
    gemini_model = (getattr(settings, "GOOGLE_API_MODEL", None) or "gemini-2.5-flash")
    gemini_error: Optional[Exception] = None

    if getattr(settings, "GEMINI_API_KEY", None):
        try:
            result = await call_llm(
                messages,
                model=gemini_model,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=True,
                system_instruction=system_instruction,
            )
            content = result if isinstance(result, str) else result.get("content", "")
            return content, False
        except Exception as e:
            gemini_error = e
            logger.warning(
                "%s: Gemini failed (%s). Fallback to %s.",
                logger_hint,
                e,
                fallback_model,
            )
    else:
        logger.warning(
            "%s: GEMINI_API_KEY not configured. Fallback to %s.",
            logger_hint,
            fallback_model,
        )

    try:
        result = await call_llm(
            messages,
            model=fallback_model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
            system_instruction=system_instruction,
        )
        content = result if isinstance(result, str) else result.get("content", "")
        return content, True
    except Exception as fallback_error:
        raise RuntimeError(
            f"{logger_hint}: Gemini and fallback model both failed "
            f"(gemini_error={gemini_error!s}, fallback_error={fallback_error!s})"
        ) from fallback_error
