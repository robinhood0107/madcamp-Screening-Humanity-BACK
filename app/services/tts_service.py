"""
TTS API/채팅 TTS 경로 공통 서비스.

[의도]
- `app/api/tts.py`의 request 보정/Server A 호출/파일 저장/캐시 저장 중복을 단계 함수로 분리한다.
- 외부 응답 shape/라우터 path는 유지하고, 내부 오케스트레이션만 얇게 만든다.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audio import AudioFile
from app.models.user import User
from app.models.voice import Voice
from app.services import server_a_client
from app.services.audio_analyzer import AudioAnalyzer

logger = logging.getLogger(__name__)


def _voice_to_dict(voice: Voice) -> Dict[str, Any]:
    return {
        "id": voice.id,
        "name": voice.name,
        "ref_audio_path": voice.ref_audio_path,
        "gpt_weights_path": voice.gpt_weights_path,
        "sovits_weights_path": voice.sovits_weights_path,
        "prompt_text": voice.prompt_text,
        "prompt_lang": voice.prompt_lang,
        "language": voice.language,
    }


def _text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


async def get_voice_from_db_dict(voice_id: str, db: AsyncSession) -> Optional[Dict[str, Any]]:
    """
    [역할]
    DB에서 active voice를 조회해 TTS 보정에 필요한 dict 형태로 반환한다.
    """
    result = await db.execute(
        select(Voice).where(Voice.id == voice_id, Voice.is_active == True)
    )
    voice = result.scalar_one_or_none()
    if not voice:
        return None
    return _voice_to_dict(voice)


def _apply_voice_defaults_to_request(request: Any, voice_data: Dict[str, Any]) -> None:
    """
    [역할]
    Voice DB 레코드 기반 기본값을 TTS request에 주입한다.
    """
    request.ref_audio_path = voice_data["ref_audio_path"]
    request.prompt_text = voice_data.get("prompt_text") or getattr(request, "prompt_text", "") or ""
    request.prompt_lang = voice_data.get("prompt_lang") or getattr(request, "prompt_lang", "ko")
    if not getattr(request, "gpt_weights_path", None):
        request.gpt_weights_path = voice_data.get("gpt_weights_path")
    if not getattr(request, "sovits_weights_path", None):
        request.sovits_weights_path = voice_data.get("sovits_weights_path")


async def resolve_tts_request_defaults(
    *,
    request: Any,
    db: AsyncSession,
    legacy_ref_audio_resolver: Callable[[Optional[str]], Optional[str]],
) -> Optional[Dict[str, Any]]:
    """
    [역할]
    `voice_id` 기반으로 DB 우선 / legacy JSON fallback 순서로 request 기본값을 보정한다.

    [주의]
    - request 객체를 in-place 수정한다.
    - `voice_id`가 없거나 `ref_audio_path`가 이미 있으면 아무것도 하지 않는다.
    """
    if not getattr(request, "voice_id", None) or getattr(request, "ref_audio_path", None):
        return None

    voice_id = request.voice_id
    voice_data = await get_voice_from_db_dict(voice_id, db)
    if voice_data:
        _apply_voice_defaults_to_request(request, voice_data)
        return voice_data

    ref_path = legacy_ref_audio_resolver(voice_id)
    if ref_path:
        request.ref_audio_path = ref_path
        return None

    raise ValueError(f"유효하지 않은 voice_id입니다: {voice_id}")


def validate_tts_request_ready(request: Any) -> None:
    """
    [역할]
    Server A 호출 전 필수 필드가 갖춰졌는지 검사한다.
    """
    if not getattr(request, "ref_audio_path", None):
        raise ValueError("참조 오디오 경로(ref_audio_path)가 필요합니다.")


async def lookup_cached_tts_audio_response(
    *,
    request: Any,
    current_user: Optional[User],
    db: AsyncSession,
) -> Optional[Dict[str, Any]]:
    """
    [역할]
    로그인 사용자에 한해 기존 TTS 결과 캐시(AudioFile)를 조회한다.
    """
    if current_user is None or getattr(request, "return_binary", False):
        return None

    text_hash = _text_hash(request.text)
    result = await db.execute(
        select(AudioFile).where(
            AudioFile.text_hash == text_hash,
            AudioFile.voice_id == (request.voice_id or "default"),
            AudioFile.format == request.media_type,
        )
    )
    cached_audio = result.scalar_one_or_none()
    if not cached_audio:
        return None

    return {
        "audio_url": cached_audio.file_url,
        "cached": True,
        "duration": cached_audio.duration,
        "format": cached_audio.format,
    }


async def call_tts_binary_or_raise(
    *,
    request: Any,
    error_prefix: str = "TTS Server Error",
) -> bytes:
    """
    [역할]
    Server A TTS를 호출하고 성공 응답 body(bytes)를 반환한다.
    """
    tts_body = request.model_dump_for_gpt_sovits()
    logger.debug(
        "TTS payload prepared: media_type=%s text_len=%s voice_id=%s",
        getattr(request, "media_type", None),
        len(getattr(request, "text", "") or ""),
        getattr(request, "voice_id", None),
    )

    response = await server_a_client.call_tts(
        json_body=tts_body,
        timeout=settings.TTS_TIMEOUT,
    )
    server_a_client.ensure_success_response(response, error_prefix=error_prefix)

    if not response.content:
        raise HTTPException(status_code=500, detail="TTS 생성 결과가 비어있습니다.")

    return response.content


def media_type_for_audio_format(audio_format: str) -> str:
    media_type_map = {
        "wav": "audio/wav",
        "ogg": "audio/ogg",
        "aac": "audio/aac",
        "raw": "audio/raw",
    }
    return media_type_map.get(audio_format, "audio/wav")


def build_public_tts_json_response(
    *,
    audio_content: bytes,
    media_type: str,
    voice_id: Optional[str],
) -> Dict[str, Any]:
    """
    [역할]
    공개 `/tts` 엔드포인트 JSON 응답용 base64 payload를 생성한다.
    """
    audio_base64 = base64.b64encode(audio_content).decode("utf-8")
    return {
        "audio_base64": audio_base64,
        "format": media_type,
        "voice_id": voice_id or "default",
    }


async def save_tts_audio_and_optionally_cache(
    *,
    audio_content: bytes,
    request: Any,
    current_user: Optional[User],
    db: AsyncSession,
) -> Dict[str, Any]:
    """
    [역할]
    TTS 결과를 파일로 저장하고(필요 시 DB 캐시 저장) API 응답용 data dict를 반환한다.

    [주의]
    - 비로그인 사용자는 파일 저장만 수행하고 DB에는 저장하지 않는다.
    - 로그인 사용자는 AudioFile을 생성/commit한다.
    """
    file_id = str(uuid.uuid4())
    file_ext = request.media_type
    max_file_size = settings.TTS_MAX_FILE_SIZE
    if len(audio_content) > max_file_size:
        raise HTTPException(status_code=413, detail="File too large")

    if current_user is None:
        audio_dir = Path(settings.USER_ASSETS_DIR) / "audio" / "anonymous"
        audio_dir.mkdir(parents=True, exist_ok=True)
        file_name = f"anon_{file_id}.{file_ext}"
        file_path = audio_dir / file_name
        file_url = f"/assets/audio/anonymous/{file_name}"
        with open(file_path, "wb") as f:
            f.write(audio_content)

        audio_info = AudioAnalyzer().analyze_audio(str(file_path))
        return {
            "audio_url": file_url,
            "file_id": file_id,
            "duration": audio_info["duration"],
            "format": audio_info["format"],
            "cached": False,
        }

    audio_dir = Path(settings.USER_ASSETS_DIR) / "audio" / current_user.id
    audio_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"{current_user.id}_{file_id}.{file_ext}"
    file_path = audio_dir / file_name
    file_url = f"/assets/audio/{current_user.id}/{file_name}"
    with open(file_path, "wb") as f:
        f.write(audio_content)

    audio_info = AudioAnalyzer().analyze_audio(str(file_path))
    audio_file = AudioFile(
        id=file_id,
        user_id=current_user.id,
        file_path=str(file_path),
        file_url=file_url,
        file_size=audio_info["file_size"],
        duration=audio_info["duration"],
        format=audio_info["format"],
        voice_id=request.voice_id or "default",
        text_hash=_text_hash(request.text),
    )
    db.add(audio_file)
    await db.commit()
    await db.refresh(audio_file)

    return {
        "audio_url": audio_file.file_url,
        "file_id": audio_file.id,
        "cached": False,
        "duration": audio_file.duration,
    }
