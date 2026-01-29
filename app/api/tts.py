"""
TTS API 엔드포인트
Server A의 GPT-SoVITS API를 직접 호출하여 TTS를 수행합니다.
가중치 변경은 /tts/prepare 에서 처리하고, 실제 TTS 생성은 /tts 또는 내부 함수에서 수행합니다.
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, validator
from typing import List, Optional, Dict, Any
import os
import json
import hashlib
import uuid
import httpx
import logging
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.config import settings
from app.api.deps import get_db
from app.models.user import User
from app.models.audio import AudioFile
from app.services.audio_analyzer import AudioAnalyzer

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
    """
    tts_base_url = settings.TTS_BASE_URL
    
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
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        if gpt_weights:
            try:
                await client.get(f"{tts_base_url}/set_gpt_weights", params={"weights_path": gpt_weights})
                logger.info(f"Loaded GPT weights for {request.voice_id}")
            except Exception as e:
                logger.error(f"Failed to set GPT weights: {e}")
                
        if sovits_weights:
            try:
                await client.get(f"{tts_base_url}/set_sovits_weights", params={"weights_path": sovits_weights})
                logger.info(f"Loaded SoVITS weights for {request.voice_id}")
            except Exception as e:
                logger.error(f"Failed to set SoVITS weights: {e}")
                
    return {"success": True, "message": f"Weights prepared for {request.voice_id}"}

# voice_id 매핑 설정 로드 (레거시)
_voices_config = None

def load_voices_config():
    """voice_id 매핑 설정 파일 로드"""
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
    """voice_id로 ref_audio_path 조회 (JSON 파일 기반 - 레거시)"""
    config = load_voices_config()
    
    if voice_id is None:
        voice_id = config.get("default_voice_id", "default")
    
    for voice in config.get("voices", []):
        if voice.get("id") == voice_id:
            return voice.get("ref_audio_path")
    
    return None


async def get_voice_from_db(voice_id: str, db: AsyncSession) -> Optional[Dict[str, Any]]:
    """DB에서 voice_id로 음성 정보 조회"""
    from app.models.voice import Voice
    
    result = await db.execute(
        select(Voice).where(Voice.id == voice_id, Voice.is_active == True)
    )
    voice = result.scalar_one_or_none()
    
    if voice:
        return {
            "id": voice.id,
            "name": voice.name,
            "ref_audio_path": voice.ref_audio_path,
            "gpt_weights_path": voice.gpt_weights_path,
            "sovits_weights_path": voice.sovits_weights_path,
            "prompt_text": voice.prompt_text,
            "prompt_lang": voice.prompt_lang,
            "language": voice.language
        }
    return None


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
    """
    try:
        # DB에서 Voice 정보 조회 및 보정
        if request.voice_id and not request.ref_audio_path:
            voice_data = await get_voice_from_db(request.voice_id, db)
            if voice_data:
                request.ref_audio_path = voice_data["ref_audio_path"]
                request.prompt_text = voice_data.get("prompt_text") or request.prompt_text
                request.prompt_lang = voice_data.get("prompt_lang") or request.prompt_lang
                if not request.gpt_weights_path:
                    request.gpt_weights_path = voice_data.get("gpt_weights_path")
                if not request.sovits_weights_path:
                    request.sovits_weights_path = voice_data.get("sovits_weights_path")
            else:
                ref_path = get_ref_audio_path(request.voice_id)
                if ref_path:
                    request.ref_audio_path = ref_path
                else:
                    raise ValueError(f"유효하지 않은 voice_id입니다: {request.voice_id}")

        if not request.ref_audio_path:
             raise ValueError("참조 오디오 경로(ref_audio_path)가 필요합니다.")

        # 캐시 체크 (로그인 유저만)
        text_hash = hashlib.sha256(request.text.encode("utf-8")).hexdigest()
        if not request.return_binary and current_user is not None:
            result = await db.execute(
                select(AudioFile).where(
                    AudioFile.text_hash == text_hash,
                    AudioFile.voice_id == (request.voice_id or "default"),
                    AudioFile.format == request.media_type
                )
            )
            cached_audio = result.scalar_one_or_none()
            if cached_audio:
                return {
                    "success": True,
                    "data": {
                        "audio_url": cached_audio.file_url,
                        "cached": True,
                        "duration": cached_audio.duration,
                        "format": cached_audio.format
                    }
                }

        # Server A TTS API 직접 호출 (Redis Queue 우회)
        # 가중치 변경 로직은 /tts/prepare 에서 처리하므로 여기서는 생략
        tts_base_url = settings.TTS_BASE_URL
        
        async with httpx.AsyncClient(timeout=settings.TTS_TIMEOUT) as client:
            # TTS 요청 (POST /tts)
            tts_body = request.model_dump_for_gpt_sovits()
            # logger.info(f"TTS 요청: text={tts_body.get('text')[:30]}...")
            
            # Debug: Request Body 확인
            print(f"[TTS DEBUG] Payload: {json.dumps(tts_body, ensure_ascii=False)}")
            
            try:
                tts_response = await client.post(
                    f"{tts_base_url}/tts",
                    json=tts_body
                )
                tts_response.raise_for_status()
                audio_content = tts_response.content
            except httpx.HTTPStatusError as e:
                print(f"[TTS ERROR] Status: {e.response.status_code}, Body: {e.response.text}")
                raise HTTPException(status_code=e.response.status_code, detail=f"TTS Server Error: {e.response.text}")
            
        if not audio_content:
            raise HTTPException(status_code=500, detail="TTS 생성 결과가 비어있습니다.")

        # 파일 저장 로직 (기존과 동일)
        file_id = str(uuid.uuid4())
        file_ext = request.media_type
        max_file_size = settings.TTS_MAX_FILE_SIZE
        if len(audio_content) > max_file_size:
             raise HTTPException(status_code=413, detail="File too large")

        if current_user is None:
            # 비로그인: anonymous
            audio_dir = Path(settings.USER_ASSETS_DIR) / "audio" / "anonymous"
            audio_dir.mkdir(parents=True, exist_ok=True)
            file_name = f"anon_{file_id}.{file_ext}"
            file_path = audio_dir / file_name
            file_url = f"/assets/audio/anonymous/{file_name}"
            with open(file_path, "wb") as f:
                f.write(audio_content)
            
            analyzer = AudioAnalyzer()
            audio_info = analyzer.analyze_audio(str(file_path))
            
            return {
                "success": True,
                "data": {
                    "audio_url": file_url,
                    "file_id": file_id,
                    "duration": audio_info["duration"],
                    "format": audio_info["format"],
                    "cached": False
                }
            }
        else:
            # 로그인 유저
            audio_dir = Path(settings.USER_ASSETS_DIR) / "audio" / current_user.id
            audio_dir.mkdir(parents=True, exist_ok=True)
            file_name = f"{current_user.id}_{file_id}.{file_ext}"
            file_path = audio_dir / file_name
            file_url = f"/assets/audio/{current_user.id}/{file_name}"
            with open(file_path, "wb") as f:
                f.write(audio_content)
                
            analyzer = AudioAnalyzer()
            audio_info = analyzer.analyze_audio(str(file_path))
            
            audio_file = AudioFile(
                id=file_id,
                user_id=current_user.id,
                file_path=str(file_path),
                file_url=file_url,
                file_size=audio_info["file_size"],
                duration=audio_info["duration"],
                format=audio_info["format"],
                voice_id=request.voice_id or "default",
                text_hash=text_hash
            )
            db.add(audio_file)
            await db.commit()
            await db.refresh(audio_file)
            
            return {
                "success": True,
                "data": {
                    "audio_url": audio_file.file_url,
                    "file_id": audio_file.id,
                    "cached": False,
                    "duration": audio_file.duration
                }
            }

    except Exception as e:
        # 에러 처리
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"TTS Error: {str(e)}")


@router.post("/tts")
async def synthesize(request: TTSRequest, db: AsyncSession = Depends(get_db)):
    """
    공개 TTS 엔드포인트.
    streaming_mode > 0 또는 return_binary=True 시 StreamingResponse 반환.
    """
    try:
        # DB 정보 보정
        if request.voice_id and not request.ref_audio_path:
            voice_data = await get_voice_from_db(request.voice_id, db)
            if voice_data:
                request.ref_audio_path = voice_data["ref_audio_path"]
                request.prompt_text = voice_data.get("prompt_text") or request.prompt_text or ""
                request.prompt_lang = voice_data.get("prompt_lang") or request.prompt_lang
                if not request.gpt_weights_path:
                    request.gpt_weights_path = voice_data.get("gpt_weights_path")
                if not request.sovits_weights_path:
                    request.sovits_weights_path = voice_data.get("sovits_weights_path")
            else:
                ref_path = get_ref_audio_path(request.voice_id)
                if ref_path:
                    request.ref_audio_path = ref_path
                else:
                    raise ValueError(f"유효하지 않은 voice_id입니다: {request.voice_id}")
        
        if not request.ref_audio_path:
            raise ValueError("ref_audio_path required")

        # Server A TTS API 직접 호출 (Redis Queue 우회)
        # 가중치 변경 로직은 /tts/prepare 에서 처리하므로 여기서는 생략하고 바로 TTS 요청
        tts_base_url = settings.TTS_BASE_URL
        
        async with httpx.AsyncClient(timeout=settings.TTS_TIMEOUT) as client:
            # TTS 요청 (POST /tts)
            tts_body = request.model_dump_for_gpt_sovits()
            # Debug: Request Body 확인
            print(f"[TTS DEBUG] Payload: {json.dumps(tts_body, ensure_ascii=False)}")

            try:
                tts_response = await client.post(
                    f"{tts_base_url}/tts",
                    json=tts_body
                )
                tts_response.raise_for_status()
                audio_content = tts_response.content
            except httpx.HTTPStatusError as e:
                print(f"[TTS ERROR] Status: {e.response.status_code}, Body: {e.response.text}")
                raise HTTPException(status_code=e.response.status_code, detail=f"TTS Server Error: {e.response.text}")
        
        # 스트리밍 응답 (바이너리 직접 반환)
        if request.return_binary:
            media_type_map = {
                "wav": "audio/wav",
                "ogg": "audio/ogg",
                "aac": "audio/aac",
                "raw": "audio/raw"
            }
            content_type = media_type_map.get(request.media_type, "audio/wav")
            
            return Response(
                content=audio_content,
                media_type=content_type,
                headers={"Content-Disposition": f"attachment; filename=tts_output.{request.media_type}"}
            )
        
        # JSON 응답 (Base64)
        import base64
        audio_base64 = base64.b64encode(audio_content).decode("utf-8")
        
        return {
            "success": True,
            "data": {
                "audio_base64": audio_base64,
                "format": request.media_type,
                "voice_id": request.voice_id or "default"
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TTS Error: {str(e)}")


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
