"""
TTS API 엔드포인트
Server A의 GPT-SoVITS API를 직접 호출하여 TTS를 수행합니다.
가중치 변경은 /tts/prepare 에서 처리하고, 실제 TTS 생성은 /tts 또는 내부 함수에서 수행합니다.
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, validator
from typing import List, Optional, Dict, Any
import json
import logging
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.config import settings
from app.api.deps import get_db
from app.models.user import User
from app.services import server_a_client, tts_service

router = APIRouter()
logger = logging.getLogger(__name__)

class TTSPrepareRequest(BaseModel):
    """가중치 로드 요청 모델"""
    voice_id: str = Field(..., description="음성 ID")

@router.post("/tts/prepare")
async def prepare_tts_weights(
    request: TTSPrepareRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    채팅 시작 전 TTS 모델 가중치를 미리 로드합니다.
    (voice_id에 해당하는 gpt_weights_path, sovits_weights_path를 Server A에 설정)

    [왜 여기서 처리하나]
    - 채팅 본 요청(`/chat`)에서 첫 TTS 호출 지연을 줄이기 위한 "선행 준비" 성격 엔드포인트다.
    - 가중치 설정 실패가 곧바로 채팅 전체 실패로 이어지지 않도록, 경고/부분 성공 정책을 유지한다.

    [주의]
    - 레거시 JSON voice config 경로는 가중치 경로를 보통 갖지 않으므로 "성공이지만 준비 스킵" 응답이 나올 수 있다.
    """
    # DB 조회
    voice_data = await get_voice_from_db(request.voice_id, db)
    if not voice_data:
        # DB에 없으면 JSON config 조회 (레거시)
        ref_path = get_ref_audio_path(request.voice_id)
        if not ref_path:
             # 유효하지 않은 voice_id라도 에러보다는 경고 로그만 남기고 성공 처리 (채팅 진행을 막지 않음)
             logger.warning(f"TTS Prepare: Voice not found for {request.voice_id}")
             return {"message": "Voice not found, skipped", "success": False}
        # JSON config엔 가중치 경로 정보가 보통 없으므로 스킵할 수도 있으나, 
        # 만약 있다면 여기서 처리. 현재 로직상 JSON엔 path 정보 없음 -> 스킵
        return {"message": "Legacy voice config used (no weights path)", "success": True}

    gpt_weights = voice_data.get("gpt_weights_path")
    sovits_weights = voice_data.get("sovits_weights_path")
    
    if gpt_weights:
        try:
            gpt_resp = await server_a_client.set_tts_gpt_weights(weights_path=gpt_weights, timeout=10.0)
            if gpt_resp.status_code == 200:
                logger.info("Loaded GPT weights for %s", request.voice_id)
            else:
                logger.error("Failed to set GPT weights: status=%s body=%s", gpt_resp.status_code, gpt_resp.text)
        except HTTPException as e:
            logger.warning("Failed to set GPT weights (request error): %s", e.detail)
        except Exception as e:
            logger.exception("Failed to set GPT weights: %s", e)

    if sovits_weights:
        try:
            sovits_resp = await server_a_client.set_tts_sovits_weights(weights_path=sovits_weights, timeout=10.0)
            if sovits_resp.status_code == 200:
                logger.info("Loaded SoVITS weights for %s", request.voice_id)
            else:
                logger.error("Failed to set SoVITS weights: status=%s body=%s", sovits_resp.status_code, sovits_resp.text)
        except HTTPException as e:
            logger.warning("Failed to set SoVITS weights (request error): %s", e.detail)
        except Exception as e:
            logger.exception("Failed to set SoVITS weights: %s", e)
                
    return {"success": True, "message": f"Weights prepared for {request.voice_id}"}

# voice_id 매핑 설정 로드 (레거시)
_voices_config = None

def load_voices_config():
    """
    [역할]
    레거시 `voices.json` 설정을 로드/캐시한다.

    [왜 여기서 처리하나]
    - DB 기반 voice 관리 이전 경로(`voice_id -> ref_audio_path`) 호환을 1차에서 유지하기 위해서다.

    [주의]
    - 파일 미존재 시 예외 대신 기본 config를 반환한다(레거시 호환).
    """
    global _voices_config
    if _voices_config is None:
        config_path = Path(__file__).parent.parent / "config" / "voices.json"
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                _voices_config = json.load(f)
        except FileNotFoundError:
            _voices_config = {
                "default_voice_id": "default",
                "voices": []
            }
    return _voices_config

def get_ref_audio_path(voice_id: Optional[str] = None) -> Optional[str]:
    """
    [역할]
    레거시 `voices.json` 기준으로 `voice_id -> ref_audio_path`를 조회한다.

    [왜 여기서 처리하나]
    - `tts_service.resolve_tts_request_defaults()`에서 DB 음성 설정이 없을 때 fallback resolver로 주입된다.

    [주의]
    - DB 우선 정책 이후의 fallback 경로이므로, 신규 기능은 이 함수에 의존하지 않도록 유지한다.
    """
    config = load_voices_config()
    
    if voice_id is None:
        voice_id = config.get("default_voice_id", "default")
    
    for voice in config.get("voices", []):
        if voice.get("id") == voice_id:
            return voice.get("ref_audio_path")
    
    return None


async def get_voice_from_db(voice_id: str, db: AsyncSession) -> Optional[Dict[str, Any]]:
    """
    [역할]
    라우터 호환용 wrapper. 실제 DB 조회/직렬화는 `tts_service`가 담당한다.
    """
    return await tts_service.get_voice_from_db_dict(voice_id, db)


class TTSRequest(BaseModel):
    """TTS 요청 모델"""
    text: str = Field(..., description="합성할 텍스트")
    text_lang: str = Field(default="ko", description="텍스트 언어")
    voice_id: Optional[str] = Field(None, description="음성 ID")
    ref_audio_path: Optional[str] = Field(None, description="참조 오디오 경로")
    prompt_lang: str = Field(default="ko", description="참조 오디오 언어")
    prompt_text: Optional[str] = Field("", description="참조 오디오 텍스트")
    aux_ref_audio_paths: Optional[List[str]] = Field(default=[], description="추가 참조 오디오")
    gpt_weights_path: Optional[str] = Field(None, description="GPT 가중치 경로")
    sovits_weights_path: Optional[str] = Field(None, description="SoVITS 가중치 경로")
    
    # 설정
    top_k: int = 5
    top_p: float = 1.0
    temperature: float = 1.0
    repetition_penalty: float = 1.35
    batch_size: int = 1
    speed_factor: float = 1.0
    seed: int = -1
    parallel_infer: bool = True
    text_split_method: str = "cut5"
    batch_threshold: float = 0.75
    split_bucket: bool = True
    media_type: str = "wav"
    streaming_mode: int = 0
    overlap_length: int = 2
    min_chunk_length: int = 16
    fragment_interval: float = 0.3
    sample_steps: int = 32
    super_sampling: bool = False
    return_binary: bool = False
    
    @validator("text")
    def validate_text(cls, v):
        if not v or not v.strip():
            raise ValueError("텍스트가 비어있습니다")
        return v.strip()
    
    def model_dump_for_gpt_sovits(self) -> Dict[str, Any]:
        """Server A로 보낼 요청 바디 (가중치 경로 제외)"""
        return self.model_dump(exclude={"voice_id", "return_binary", "gpt_weights_path", "sovits_weights_path"})


async def _synthesize_tts_internal(
    request: TTSRequest,
    current_user: Optional[User],
    db: AsyncSession
) -> Dict[str, Any]:
    """
    TTS 합성 내부 함수 (Chat API 등에서 사용).
    항상 전체 오디오를 생성하여 파일로 저장하고 URL을 반환합니다.
    (Worker Queue를 경유하여 모델 로드 순서를 보장받습니다.)

    [왜 여기서 처리하나]
    - `chat.py` 등 내부 호출자가 `/tts` 공개 라우터의 binary/json 분기 로직을 알 필요 없이
      "성공 시 저장된 audio_url 반환" 계약만 사용하게 하기 위한 내부 오케스트레이션 wrapper다.

    [부작용]
    - DB 조회(voice lookup/cache)
    - 외부 HTTP(Server A TTS)
    - 파일 저장/오디오 분석/DB 캐시 저장 (service 경유)

    [실패/예외]
    - `HTTPException`은 그대로 올리고, `ValueError`는 400, 나머지는 500으로 매핑한다.
      (기존 `TTS Error: ...` detail prefix 계약 유지)

    [주의]
    - 반환 shape는 `{"success": True, "data": ...}` 고정. `chat.py`가 이 구조를 직접 참조한다.
    """
    try:
        await tts_service.resolve_tts_request_defaults(
            request=request,
            db=db,
            legacy_ref_audio_resolver=get_ref_audio_path,
        )
        tts_service.validate_tts_request_ready(request)

        cached = await tts_service.lookup_cached_tts_audio_response(
            request=request,
            current_user=current_user,
            db=db,
        )
        if cached:
            return {"success": True, "data": cached}

        audio_content = await tts_service.call_tts_binary_or_raise(
            request=request,
            error_prefix="TTS Server Error",
        )
        data = await tts_service.save_tts_audio_and_optionally_cache(
            audio_content=audio_content,
            request=request,
            current_user=current_user,
            db=db,
        )
        return {"success": True, "data": data}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"TTS Error: {str(e)}") from e
    except Exception as e:
        logger.exception("TTS internal synthesis failed: %s", e)
        raise HTTPException(status_code=500, detail=f"TTS Error: {str(e)}") from e


@router.post("/tts")
async def synthesize(request: TTSRequest, db: AsyncSession = Depends(get_db)):
    """
    공개 TTS 엔드포인트.
    streaming_mode > 0 또는 return_binary=True 시 StreamingResponse 반환.

    [왜 내부 helper와 분리하나]
    - 공개 라우터는 transport 역할(요청 검증, binary/json 응답 분기, HTTPException 매핑)만 담당하고,
      실제 합성/파일 저장/캐시 로직은 `tts_service` helper로 위임한다.

    [주의]
    - `/tts`는 `return_binary` 여부에 따라 응답 형태가 달라지므로, 내부 helper 반환을 그대로 노출하지 않는다.
    """
    try:
        await tts_service.resolve_tts_request_defaults(
            request=request,
            db=db,
            legacy_ref_audio_resolver=get_ref_audio_path,
        )
        tts_service.validate_tts_request_ready(request)

        audio_content = await tts_service.call_tts_binary_or_raise(
            request=request,
            error_prefix="TTS Server Error",
        )

        if request.return_binary:
            return Response(
                content=audio_content,
                media_type=tts_service.media_type_for_audio_format(request.media_type),
                headers={"Content-Disposition": f"attachment; filename=tts_output.{request.media_type}"},
            )

        return {
            "success": True,
            "data": tts_service.build_public_tts_json_response(
                audio_content=audio_content,
                media_type=request.media_type,
                voice_id=request.voice_id,
            ),
        }
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"TTS Error: {str(e)}") from e
    except Exception as e:
        logger.exception("Public TTS synthesis failed: %s", e)
        raise HTTPException(status_code=500, detail=f"TTS Error: {str(e)}") from e


@router.get("/tts/voices")
async def list_voices(db: AsyncSession = Depends(get_db)):
    """사용 가능한 음성 목록 조회"""
    from app.models.voice import Voice
    result = await db.execute(
        select(Voice).where(Voice.is_active == True).order_by(Voice.is_default.desc(), Voice.name)
    )
    db_voices = result.scalars().all()
    
    voices = []
    default_voice_id = None
    for voice in db_voices:
        voices.append({
            "id": voice.id,
            "name": voice.name,
            "language": voice.language,
            "description": voice.description or ""
        })
        if voice.is_default:
            default_voice_id = voice.id
            
    return {
        "success": True,
        "data": {
            "voices": voices,
            "default_voice_id": default_voice_id
        }
    }
