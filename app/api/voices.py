"""
Voice 관리 API 엔드포인트
음성 목록 조회, 등록, 수정, 삭제 및 테스트 기능을 제공합니다.
"""
from fastapi import APIRouter, Depends, HTTPException, status, Form, UploadFile, File
from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import uuid
import logging
from pathlib import Path

from app.api.deps import get_db, get_current_user, require_admin
from app.models.voice import Voice
from app.models.user import User
from app.core.config import settings
from app.services import server_a_client

router = APIRouter()
logger = logging.getLogger(__name__)


# ============ 요청/응답 모델 ============

class VoiceBase(BaseModel):
    """음성 기본 정보"""
    name: str = Field(..., min_length=1, max_length=100, description="음성 이름")
    description: Optional[str] = Field(None, description="음성 설명")
    language: str = Field(default="ko", description="언어 코드")
    ref_audio_path: str = Field(..., description="Server A 내부 참조 오디오 경로")
    prompt_text: Optional[str] = Field(default="", description="참조 오디오의 텍스트")
    prompt_lang: str = Field(default="ko", description="참조 오디오 언어")
    # GPT-SoVITS Fine-tuned 모델 설정
    gpt_weights_path: Optional[str] = Field(None, description="GPT 모델 경로")
    sovits_weights_path: Optional[str] = Field(None, description="SoVITS 모델 경로")
    model_version: str = Field(default="v2", description="모델 버전")
    train_voice_folder: Optional[str] = Field(None, description="훈련 음성 폴더명")
    is_default: bool = Field(default=False, description="기본 음성 여부")
    is_active: bool = Field(default=True, description="활성화 상태")

    model_config = ConfigDict(protected_namespaces=())


class VoiceCreateRequest(VoiceBase):
    """음성 생성 요청"""
    train_input_dir: Optional[str] = Field(None, description="훈련 업로드 경로 (관리자 학습·DB연동용)")
    training_model_name: Optional[str] = Field(None, description="학습 model_name (logs 삭제용)")


class VoiceUpdateRequest(BaseModel):
    """음성 수정 요청 (모든 필드 선택적)"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None
    language: Optional[str] = None
    ref_audio_path: Optional[str] = None
    prompt_text: Optional[str] = None
    prompt_lang: Optional[str] = None
    gpt_weights_path: Optional[str] = None
    sovits_weights_path: Optional[str] = None
    model_version: Optional[str] = None
    train_voice_folder: Optional[str] = None
    is_default: Optional[bool] = None
    is_active: Optional[bool] = None

    model_config = ConfigDict(protected_namespaces=())


class VoiceResponse(BaseModel):
    """음성 응답 모델"""
    id: str
    name: str
    description: Optional[str]
    language: str
    ref_audio_path: str
    prompt_text: Optional[str]
    prompt_lang: str
    gpt_weights_path: Optional[str] = None
    sovits_weights_path: Optional[str] = None
    model_version: Optional[str] = None
    train_voice_folder: Optional[str] = None
    train_input_dir: Optional[str] = None   # 모델 제작 업로드 경로 (user_{id}/run_{ts})
    training_model_name: Optional[str] = None  # 학습 model_name (logs 삭제용)
    is_default: bool
    is_active: bool
    user_id: Optional[str] = None  # 소유자 (None=시스템)
    created_at: Optional[str]
    updated_at: Optional[str]

    model_config = ConfigDict(protected_namespaces=(), from_attributes=True)


class VoiceLinkOption(BaseModel):
    """캐릭터 voice_id 선택용 { id, name, gpt_weights_path?, sovits_weights_path? }"""
    id: str
    name: str
    gpt_weights_path: Optional[str] = None
    sovits_weights_path: Optional[str] = None


class VoiceListResponse(BaseModel):
    """음성 목록 응답"""
    voices: List[VoiceResponse]
    total: int


class VoiceTestRequest(BaseModel):
    """음성 테스트 요청"""
    text: str = Field(default="안녕하세요, 반갑습니다.", description="테스트 텍스트")


# ============ Server A 파일 관리 API (관리자, 순서 중요: ID 매칭 방지 위해 상단 배치) ============

@router.get("/voices/server-files")
async def get_server_files(
    current_user: User = Depends(require_admin)
):
    """
    Server A의 GPT-SoVITS 파일 목록 조회 (관리자 전용)
    
    Server A의 파일 스캔 API를 호출하여 모델, 훈련 음성, 참조 오디오 목록을 반환합니다.
    URL: /api/files/all
    """
    # [왜 여기서 처리하나]
    # 관리자 파일 매니저는 "현재 파일 트리 전체"를 한 번에 받아야 UI 초기 렌더가 단순해진다.
    # 다만 Server A 버전별로 통합 인덱스 API 지원 여부가 달라서 라우터에서 호환 폴백을 유지한다.
    #
    # [주의]
    # 반환 키(models/train_voices/logs)는 프론트 파일 패널 파서와 직접 연결되어 있어 변경하면 안 된다.
    try:
        response = await server_a_client.get_files_index(timeout=10.0)
        if response.status_code == 200:
            return response.json()

        # API가 통합 엔드포인트를 지원하지 않는 경우 개별 조회 (models, train_voices, logs)
        models_res = await server_a_client.get_files_models(timeout=10.0)
        train_voices_res = await server_a_client.get_files_train_voices(timeout=10.0)
        logs_res = await server_a_client.get_files_logs(timeout=10.0)
        return {
            "models": models_res.json() if models_res.status_code == 200 else {},
            "train_voices": train_voices_res.json() if train_voices_res.status_code == 200 else {},
            "logs": logs_res.json() if logs_res.status_code == 200 else {"models": []}
        }
    except HTTPException as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=f"Server A 파일 API({settings.SERVER_A_FILES_API_URL})에 연결할 수 없습니다: {e.detail}"
        )


# 허용 오디오 확장자 (train_voice 전용 업로드)
_ALLOWED_AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg"}


async def process_ref_audio(path: str, train_input_dir: Optional[str] = None) -> str:
    """
    Server A에 오디오 파일 검증 요청.
    - 3~10초: 통과 (원본 경로 반환)
    - 10초 초과: 자동 트리밍 시도 (prepare-ref-audio 호출) -> 성공 시 변경된 경로 반환
    - 3초 미만: 에러 발생
    """
    # [왜 여기서 처리하나]
    # create/update/test 경로가 모두 같은 길이 정책을 공유하므로 라우터 밖 helper로 모아 정책 드리프트를 막는다.
    #
    # [주의]
    # 반환값은 "Server A 내부 경로"이다. 로컬 경로처럼 열거나 stat 하면 안 된다.
    # 1. 검증 요청
    try:
        resp = await server_a_client.validate_ref_audio(path=path, timeout=10.0)
        if resp.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Audio validation API failed: {resp.text}")

        data = resp.json()
        is_valid = data.get("valid")
        duration = data.get("duration_sec", 0.0)

        if is_valid:
            return path

        # 2. 유효하지 않은 경우 처리
        if duration < 3.0:
            raise HTTPException(
                status_code=400,
                detail=f"참조 오디오가 너무 짧습니다 ({duration}s). 최소 3초 이상이어야 합니다."
            )

        # 10초 초과 -> 자동 트리밍
        if duration > 10.0:
            if not train_input_dir:
                # train_input_dir 없을 경우 경로에서 추론 시도
                try:
                    p = Path(path)
                    if "sample_train_voice" in path:
                        train_input_dir = p.parent.name
                    else:
                        raise ValueError("Cannot infer train_input_dir")
                except Exception:
                    raise HTTPException(status_code=400, detail="오디오가 10초를 초과하여 자르기가 필요하지만 train_input_dir 정보가 부족합니다.")

            ref_file_name = Path(path).name
            prepare_resp = await server_a_client.prepare_ref_audio(
                data={
                    "sub_path": train_input_dir,
                    "ref_file": ref_file_name,
                    "max_duration_sec": 10.0,
                },
                timeout=30.0,
            )

            if prepare_resp.status_code == 200:
                prep_data = prepare_resp.json()
                if prep_data.get("success"):
                    new_filename = prep_data.get("ref_audio_file")
                    parent_dir = str(Path(path).parent)
                    return f"{parent_dir}/{new_filename}".replace("\\", "/")
                raise HTTPException(status_code=400, detail=f"Auto-trimming failed: {prep_data.get('message')}")

            raise HTTPException(status_code=400, detail="Auto-trimming API request failed.")

        raise HTTPException(status_code=400, detail=f"Audio validation failed: {data.get('message')}")

    except HTTPException as e:
        if e.status_code in {400}:
            raise
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Server A connection failed: {e.detail if isinstance(e.detail, str) else str(e.detail)}"
        )


@router.post("/voices/server-files/upload")
async def upload_train_voice_file(
    file: UploadFile = File(...),
    sub_path: str = Form(..., description="sample_train_voice 하위 폴더명(캐릭터명)"),
    current_user: User = Depends(require_admin)
):
    """
    훈련 데이터(train_voice) 전용 업로드. 오디오(.wav, .mp3, .flac, .ogg)만 허용.
    Server A /api/files/upload로 category=train_voice, sub_path 전달.
    """
    # [왜 여기서 처리하나]
    # 확장자 검증은 네트워크 요청 전에 빠르게 끊어서 관리자 파일 매니저 UX(즉시 오류 표시)를 지킨다.
    #
    # [실패/예외]
    # HTTPException은 기존 상세 메시지를 그대로 유지하고, 비정형 예외만 500으로 감싼다.
    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="파일명이 없습니다")
    ext = "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in _ALLOWED_AUDIO_EXT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"오디오 파일만 허용됩니다: {', '.join(_ALLOWED_AUDIO_EXT)}"
        )
    try:
        response = await server_a_client.upload_train_voice_file(
            file_tuple=(file.filename, file.file, file.content_type or "application/octet-stream"),
            sub_path=sub_path,
            timeout=300.0,
        )
        server_a_client.ensure_success_response(response, error_prefix="Upload Failed")
        return response.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"업로드 오류: {str(e)}")


@router.delete("/voices/server-files")
async def delete_server_file(
    path: str,
    current_user: User = Depends(require_admin)
):
    """Server A 파일/폴더 삭제 (관리자 전용)"""
    try:
        response = await server_a_client.delete_file(path=path, timeout=10.0)
        server_a_client.ensure_success_response(response, error_prefix="Delete Failed")
        return response.json()
    except HTTPException:
        raise


@router.post("/voices/server-files/mkdir")
async def create_server_folder(
    path: str = Form(...),
    current_user: User = Depends(require_admin)
):
    """Server A 훈련 음성 폴더 생성 (관리자 전용)"""
    try:
        response = await server_a_client.mkdir(path=path, timeout=10.0)
        server_a_client.ensure_success_response(response, error_prefix="Mkdir Failed")
        return response.json()
    except HTTPException:
        raise


@router.post("/voices/server-files/trim")
async def trim_server_audio(
    path: str = Form(..., description="원본 파일 경로 (Server A 기준)"),
    max_duration_sec: float = Form(9.0, description="목표 길이 (초)"),
    current_user: User = Depends(require_admin)
):
    """Server A 오디오 자르기 (관리자 전용)"""
    try:
        response = await server_a_client.trim_audio(
            source_path=path,
            max_duration_sec=max_duration_sec,
            output_suffix="_trimmed",
            timeout=30.0,
        )
        server_a_client.ensure_success_response(response, error_prefix="Trim Failed")
        return response.json()
    except HTTPException:
        raise


# ============ 사용자 API (get_current_user, /voices/{id} 보다 먼저 정의) ============

@router.get("/voices/link-options", response_model=List[VoiceLinkOption])
async def get_voice_link_options(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """캐릭터 voice_id 선택용: DB Voice만. { id, name, gpt_weights_path?, sovits_weights_path? }"""
    q = select(Voice).where(
        (Voice.user_id == current_user.id) | (Voice.user_id.is_(None))
    ).where(Voice.is_active == True).order_by(Voice.name)
    res = await db.execute(q)
    rows = res.scalars().all()
    return [
        VoiceLinkOption(
            id=v.id,
            name=v.name,
            gpt_weights_path=v.gpt_weights_path,
            sovits_weights_path=v.sovits_weights_path
        )
        for v in rows
    ]


# ============ 공개 API (인증 불필요) ============

@router.get("/voices", response_model=VoiceListResponse)
async def list_voices(
    active_only: bool = True,
    db: AsyncSession = Depends(get_db)
):
    """
    활성화된 음성 목록 조회 (공개 API)
    
    - active_only=True: 활성화된 음성만 조회 (기본값)
    - active_only=False: 모든 음성 조회 (관리자용)
    """
    query = select(Voice)
    if active_only:
        query = query.where(Voice.is_active == True)
    query = query.order_by(Voice.is_default.desc(), Voice.name)
    
    result = await db.execute(query)
    voices = result.scalars().all()
    
    voice_responses = []
    for voice in voices:
        voice_responses.append(VoiceResponse(
            id=voice.id,
            name=voice.name,
            description=voice.description,
            language=voice.language,
            ref_audio_path=voice.ref_audio_path,
            prompt_text=voice.prompt_text,
            prompt_lang=voice.prompt_lang,
            gpt_weights_path=voice.gpt_weights_path,
            sovits_weights_path=voice.sovits_weights_path,
            model_version=voice.model_version,
            train_voice_folder=voice.train_voice_folder,
            train_input_dir=getattr(voice, "train_input_dir", None),
            training_model_name=getattr(voice, "training_model_name", None),
            is_default=voice.is_default,
            is_active=voice.is_active,
            user_id=getattr(voice, "user_id", None),
            created_at=voice.created_at.isoformat() if voice.created_at else None,
            updated_at=voice.updated_at.isoformat() if voice.updated_at else None,
        ))
    
    return VoiceListResponse(voices=voice_responses, total=len(voice_responses))


@router.get("/voices/{voice_id}", response_model=VoiceResponse)
async def get_voice(
    voice_id: str,
    db: AsyncSession = Depends(get_db)
):
    """특정 음성 조회"""
    result = await db.execute(
        select(Voice).where(Voice.id == voice_id)
    )
    voice = result.scalar_one_or_none()
    
    if not voice:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"음성을 찾을 수 없습니다: {voice_id}"
        )
    
    return VoiceResponse(
        id=voice.id,
        name=voice.name,
        description=voice.description,
        language=voice.language,
        ref_audio_path=voice.ref_audio_path,
        prompt_text=voice.prompt_text,
        prompt_lang=voice.prompt_lang,
        gpt_weights_path=voice.gpt_weights_path,
        sovits_weights_path=voice.sovits_weights_path,
        model_version=voice.model_version,
        train_voice_folder=voice.train_voice_folder,
        train_input_dir=getattr(voice, "train_input_dir", None),
        training_model_name=getattr(voice, "training_model_name", None),
        is_default=voice.is_default,
        is_active=voice.is_active,
        user_id=getattr(voice, "user_id", None),
        created_at=voice.created_at.isoformat() if voice.created_at else None,
        updated_at=voice.updated_at.isoformat() if voice.updated_at else None,
    )


# ============ 관리자 API (인증 필요) ============

@router.post("/voices", response_model=VoiceResponse)
async def create_voice(
    request: VoiceCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    """
    새 음성 등록 (관리자 전용)
    
    - ref_audio_path: Server A 내부의 참조 오디오 파일 경로
    - is_default=True로 설정 시 기존 기본 음성은 자동으로 False로 변경
    """
    # [왜 여기서 처리하나]
    # Voice DB 등록 직전에 ref_audio 정책(길이 검증/자동 트리밍)을 강제해, 이후 TTS 경로의 실패를 앞단에서 줄인다.
    #
    # [주의]
    # 기본 음성 교체 규칙(is_default 전환)은 관리자 UI 전제라 유지해야 한다.
    # is_default가 True면 기존 기본 음성 해제
    if request.is_default:
        await db.execute(
            Voice.__table__.update().where(Voice.is_default == True).values(is_default=False)
        )
    
    # ref_audio 검증 및 자동 처리 (Server A 호출)
    # create 시에는 request에 train_input_dir가 있을 수도 없을 수도 있음.
    # 하지만 file-manager.tsx에서는 계산해서 보냄.
    final_ref_path = await process_ref_audio(request.ref_audio_path, request.train_input_dir)
    request.ref_audio_path = final_ref_path
    
    # 새 음성 생성
    voice = Voice(
        id=str(uuid.uuid4()),
        name=request.name,
        description=request.description,
        language=request.language,
        ref_audio_path=request.ref_audio_path,
        prompt_text=request.prompt_text or "",
        prompt_lang=request.prompt_lang,
        gpt_weights_path=request.gpt_weights_path,
        sovits_weights_path=request.sovits_weights_path,
        model_version=request.model_version or "v2",
        train_voice_folder=request.train_voice_folder,
        train_input_dir=getattr(request, "train_input_dir", None),
        training_model_name=getattr(request, "training_model_name", None),
        is_default=request.is_default,
        is_active=request.is_active
    )
    
    db.add(voice)
    await db.commit()
    await db.refresh(voice)
    
    return VoiceResponse(
        id=voice.id,
        name=voice.name,
        description=voice.description,
        language=voice.language,
        ref_audio_path=voice.ref_audio_path,
        prompt_text=voice.prompt_text,
        prompt_lang=voice.prompt_lang,
        gpt_weights_path=voice.gpt_weights_path,
        sovits_weights_path=voice.sovits_weights_path,
        model_version=voice.model_version,
        train_voice_folder=voice.train_voice_folder,
        train_input_dir=getattr(voice, "train_input_dir", None),
        training_model_name=getattr(voice, "training_model_name", None),
        is_default=voice.is_default,
        is_active=voice.is_active,
        user_id=getattr(voice, "user_id", None),
        created_at=voice.created_at.isoformat() if voice.created_at else None,
        updated_at=voice.updated_at.isoformat() if voice.updated_at else None
    )


@router.put("/voices/{voice_id}", response_model=VoiceResponse)
async def update_voice(
    voice_id: str,
    request: VoiceUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    """음성 정보 수정 (관리자 전용)"""
    # [왜 여기서 처리하나]
    # update는 "부분 수정"이 핵심이므로 ref_audio_path만 조건부 재검증하고 나머지는 model_dump 결과를 그대로 반영한다.
    #
    # [주의]
    # ref_audio_path 변경 시 train_input_dir 추론 로직이 개입하므로 단순 setattr로 바꾸면 Server A 경로 정책이 깨질 수 있다.
    result = await db.execute(
        select(Voice).where(Voice.id == voice_id)
    )
    voice = result.scalar_one_or_none()
    
    if not voice:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"음성을 찾을 수 없습니다: {voice_id}"
        )
    
    # is_default가 True로 변경되면 기존 기본 음성 해제
    if request.is_default is True and not voice.is_default:
        await db.execute(
            Voice.__table__.update().where(
                Voice.is_default == True,
                Voice.id != voice_id
            ).values(is_default=False)
        )
    
    # ref_audio_path 변경 시 검증
    # ref_audio_path 변경 시 검증 및 자동 처리
    if request.ref_audio_path and request.ref_audio_path != voice.ref_audio_path:
        # train_input_dir 확보
        target_dir = request.train_input_dir or getattr(voice, "train_input_dir", None)
        final_ref_path = await process_ref_audio(request.ref_audio_path, target_dir)
        request.ref_audio_path = final_ref_path
    
    # 업데이트할 필드만 적용
    update_data = request.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(voice, key, value)
    
    await db.commit()
    await db.refresh(voice)
    
    return VoiceResponse(
        id=voice.id,
        name=voice.name,
        description=voice.description,
        language=voice.language,
        ref_audio_path=voice.ref_audio_path,
        prompt_text=voice.prompt_text,
        prompt_lang=voice.prompt_lang,
        gpt_weights_path=voice.gpt_weights_path,
        sovits_weights_path=voice.sovits_weights_path,
        model_version=voice.model_version,
        train_voice_folder=voice.train_voice_folder,
        train_input_dir=getattr(voice, "train_input_dir", None),
        training_model_name=getattr(voice, "training_model_name", None),
        is_default=voice.is_default,
        is_active=voice.is_active,
        user_id=getattr(voice, "user_id", None),
        created_at=voice.created_at.isoformat() if voice.created_at else None,
        updated_at=voice.updated_at.isoformat() if voice.updated_at else None
    )


@router.delete("/voices/{voice_id}")
async def delete_voice(
    voice_id: str,
    permanent: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    """
    음성 삭제 (관리자 전용)
    
    - permanent=False: 비활성화만 (기본값)
    - permanent=True: 완전 삭제 (연결된 캐릭터의 voice_id가 NULL로 변경됨)
    """
    result = await db.execute(
        select(Voice).where(Voice.id == voice_id)
    )
    voice = result.scalar_one_or_none()
    
    if not voice:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"음성을 찾을 수 없습니다: {voice_id}"
        )
    
    if permanent:
        # 완전 삭제
        await db.delete(voice)
        await db.commit()
        return {"success": True, "message": f"음성 '{voice.name}'이(가) 완전히 삭제되었습니다"}
    else:
        # 비활성화
        voice.is_active = False
        await db.commit()
        return {"success": True, "message": f"음성 '{voice.name}'이(가) 비활성화되었습니다"}


@router.post("/voices/{voice_id}/test")
async def test_voice(
    voice_id: str,
    request: VoiceTestRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    음성 테스트 (TTS 생성)
    
    지정된 음성으로 테스트 텍스트를 TTS 변환하여 base64 오디오 반환
    """
    # [왜 여기서 처리하나]
    # /tts 라우터를 재사용하지 않고 여기서 직접 payload를 만드는 이유는 관리자 미리듣기 UX가 base64 응답을 바로 기대하기 때문이다.
    #
    # [주의]
    # success/data/audio_base64/format/... 키는 프론트 미리듣기 파서와 연결되어 있어 shape 변경 금지.
    # 음성 조회
    result = await db.execute(
        select(Voice).where(Voice.id == voice_id, Voice.is_active == True)
    )
    voice = result.scalar_one_or_none()
    
    if not voice:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"음성을 찾을 수 없습니다: {voice_id}"
        )
    
    # GPT-SoVITS API 호출
    tts_request = {
        "text": request.text,
        "text_lang": voice.language,
        "ref_audio_path": voice.ref_audio_path,
        "prompt_text": voice.prompt_text or "",
        "prompt_lang": voice.prompt_lang,
        "streaming_mode": 0,
        "media_type": "wav"
    }
    
    # 파인튜닝 모델 가중치 추가
    if voice.gpt_weights_path:
        tts_request["gpt_weights"] = voice.gpt_weights_path
    if voice.sovits_weights_path:
        tts_request["sovits_weights"] = voice.sovits_weights_path
    
    response = await server_a_client.call_tts(json_body=tts_request, timeout=settings.TTS_TIMEOUT)
    server_a_client.ensure_success_response(response, error_prefix="TTS 서비스 오류")
    audio_content = response.content

    import base64
    audio_base64 = base64.b64encode(audio_content).decode("utf-8")

    return {
        "success": True,
        "data": {
            "audio_base64": audio_base64,
            "format": "wav",
            "voice_id": voice.id,
            "voice_name": voice.name,
            "text": request.text
        }
    }


# ============ 학습 API (Server A Proxy) ============

class TrainStartRequest(BaseModel):
    """학습 시작 요청"""
    model_name: str
    upload_path: str
    version: str = "v2"
    batch_size: int = 11
    total_epochs: int = 8
    save_every_epoch: int = 4
    gpu_numbers: str = "0-0"
    dry_run: bool = False

    model_config = ConfigDict(protected_namespaces=())

@router.post("/voices/train/start")
async def start_training_proxy(
    request: TrainStartRequest,
    current_user: User = Depends(require_admin)
):
    """
    학습 시작 (파록시)
    
    Server A의 /api/train/start 엔드포인트를 호출합니다.
    """
    # [왜 여기서 처리하나]
    # 이 경로는 "관리자 학습 프록시" 역할만 수행한다. 세부 학습 정책/파라미터 검증은 Server A가 담당한다.
    response = await server_a_client.start_training(payload=request.dict(), timeout=10.0)
    server_a_client.ensure_success_response(response, error_prefix="Training Start Failed")
    return response.json()


@router.get("/voices/train/status/{model_name}")
async def get_training_status_proxy(
    model_name: str,
    current_user: User = Depends(require_admin)
):
    """
    학습 상태 조회 (파록시)
    
    Server A의 /api/train/status/{model_name} 호출
    """
    # [주의]
    # 404는 polling 중 흔한 상태라 완전 실패로 취급하지 않고 allow_404_detail로 메시지만 고정한다.
    response = await server_a_client.get_training_status(model_name=model_name, timeout=5.0)
    server_a_client.ensure_success_response(
        response,
        error_prefix="Status Check Failed",
        allow_404_detail="Training status not found",
    )
    return response.json()


@router.get("/voices/train/log/{model_name}")
async def get_training_log_proxy(
    model_name: str,
    current_user: User = Depends(require_admin)
):
    """
    학습 로그 조회 (파록시)
    
    Server A의 /api/train/log/{model_name} 호출
    """
    # [주의]
    # 로그 파일 미생성/정리 직후 404는 정상 시나리오일 수 있으므로 allow_404_detail 정책을 유지한다.
    response = await server_a_client.get_training_log(model_name=model_name, timeout=5.0)
    server_a_client.ensure_success_response(
        response,
        error_prefix="Log Retrieval Failed",
        allow_404_detail="Log not found",
    )
    return response.json()
