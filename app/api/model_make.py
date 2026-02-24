"""
모델 제작 API (model-make)
사용자 WAV 업로드 → Server A 훈련 → Voice 등록. 업로드 검증: 3개 이상 .wav, 총 100MB, 각 5초 이상(mutagen).
"""
import io
import uuid
import time
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, status, UploadFile
from pydantic import BaseModel, Field
from mutagen.wave import WAVE

from app.api.deps import get_db, get_current_user
from app.core.config import settings
from app.models.voice import Voice
from app.models.user import User
from app.models.character import Character
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.services import server_a_client

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_TOTAL_BYTES = 100 * 1024 * 1024  # 100MB
MIN_DURATION_SEC = 5.0


# ============ 요청/응답 모델 ============

class ModelMakeStartRequest(BaseModel):
    model_name: str = Field(..., min_length=1, description="학습 모델명")
    train_input_dir: str = Field(..., description="업로드 응답의 train_input_dir")
    version: str = Field(default="v2", description="모델 버전 (v2, v2Pro 등)")
    
    model_config = {"protected_namespaces": ()}


class ModelMakeAbortRequest(BaseModel):
    train_input_dir: str = Field(..., description="업로드 시 받은 train_input_dir (삭제 대상)")
    model_name: Optional[str] = Field(None, description="학습 시작한 모델명 (logs, TEMP 삭제용, 미시작이면 생략)")
    
    model_config = {"protected_namespaces": ()}


class ModelMakeRegisterRequest(BaseModel):
    model_name: str = Field(..., description="학습 모델명 (training_model_name)")
    voice_name: str = Field(..., min_length=1, description="등록할 음성 이름")
    train_input_dir: str = Field(..., description="업로드 시 받은 train_input_dir")
    ref_audio_file: str = Field(..., description="참조용 WAV 파일명 (train_input_dir 내)")
    gpt_weights_path: Optional[str] = Field(None, description="GPT 가중치 경로 (선택)")
    sovits_weights_path: Optional[str] = Field(None, description="SoVITS 가중치 경로 (선택)")

    model_config = {"protected_namespaces": ()}


def _get_duration_sec(data: bytes, filename: str) -> Optional[float]:
    """mutagen으로 WAV 재생시간(초) 반환. 실패 시 None."""
    # [왜 여기서 처리하나]
    # 업로드 검증에서 "읽을 수 없는 WAV"를 빠르게 걸러야 하므로 라우터 바로 옆의 순수 helper로 둔다.
    # 실패 시 None을 반환해 호출부가 400 응답 메시지를 일관되게 구성한다.
    try:
        info = WAVE(io.BytesIO(data)).info
        return info.length if info else None
    except Exception:
        return None


async def _resolve_ref_audio_file_with_prepare_fallback(
    *,
    train_input_dir: str,
    original_ref_audio_file: str,
) -> str:
    """
    [역할]
    Server A `prepare-ref-audio`를 시도하고 실패하면 원본 ref_audio 파일명을 유지한다.

    [왜 helper로 분리하나]
    - `/register`에서 "실패해도 계속 진행" 정책은 유지하되, 로깅/예외 흡수 규칙을 한곳에 고정해
      오케스트레이션 본문 길이와 중복을 줄인다.

    [주의]
    - non-200/연결 실패/파싱 오류 모두 원본 파일명으로 폴백한다. (기존 UX 계약 유지)
    """
    try:
        prepare_resp = await server_a_client.prepare_ref_audio(
            data={
                "train_input_dir": train_input_dir,
                "max_duration_sec": 10.0,  # 10초로 제한
            },
            timeout=30.0,
        )
        if prepare_resp.status_code != 200:
            logger.warning("prepare-ref-audio failed: %s", prepare_resp.text)
            return original_ref_audio_file

        prepare_data = prepare_resp.json()
        if prepare_data.get("success"):
            logger.info("ref_audio prepared: %s", prepare_data)
            return prepare_data.get("ref_audio_file", original_ref_audio_file)

        return original_ref_audio_file
    except HTTPException as e:
        logger.warning("prepare-ref-audio error: %s", e.detail)
        return original_ref_audio_file
    except Exception as e:
        logger.warning("prepare-ref-audio error: %s", e)
        return original_ref_audio_file


# ============ POST /upload ============

@router.post("/upload")
async def upload_training_files(
    files: List[UploadFile] = File(..., description="WAV 파일 3개 이상 (files 또는 files[])"),
    current_user: User = Depends(get_current_user),
):
    """
    훈련용 WAV 업로드.
    - 3개 이상, .wav만, 한 번에 총 100MB 이하, 각 5초 이상(mutagen). 실패 시 400.
    - Server A sample_train_voice/{user_id}/run_{ts} 형태로 저장.
    """
    # [왜 여기서 처리하나]
    # 업로드 전 검증(개수/확장자/총용량/길이)을 라우터에서 끝내면 Server A에 불필요한 네트워크/디스크 작업을 줄일 수 있다.
    #
    # [주의]
    # 반환값의 `train_input_dir`, `first_file`은 이후 /start, /register 흐름에서 그대로 이어진다.
    if len(files) < 3:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="WAV 파일은 3개 이상이어야 합니다.",
        )

    # 확장자 .wav만
    bad = [f.filename or "?" for f in files if not (f.filename or "").lower().endswith(".wav")]
    if bad:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f".wav만 업로드 가능합니다. 비허용: {', '.join(bad)}",
        )

    # 읽기 및 총 용량
    items: List[tuple] = []
    total = 0
    for f in files:
        raw = await f.read()
        n = len(raw)
        total += n
        items.append((f.filename, raw))

    if total > MAX_TOTAL_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"한 번에 올리는 훈련용 파일 총합은 100MB 이하여야 합니다. 현재: {total / (1024*1024):.2f}MB",
        )

    # 각 5초 이상 (mutagen)
    short: List[str] = []
    unreadable: List[str] = []
    for name, data in items:
        d = _get_duration_sec(data, name)
        if d is None:
            unreadable.append(name)
        elif d < MIN_DURATION_SEC:
            short.append(name)

    if unreadable:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"다음 파일의 재생시간을 확인할 수 없습니다: {', '.join(unreadable)}",
        )
    if short:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"다음 파일이 5초 미만입니다: {', '.join(short)}",
        )

    # Server A 업로드: user_{id}/run_{ts}
    ts = int(time.time_ns() // 1000)
    sub_path = f"user_{current_user.id}/run_{ts}"
    root = getattr(settings, "SERVER_A_TRAIN_VOICE_ROOT", "/opt/GPT-SoVITS/sample_train_voice")
    first_file: Optional[str] = None
    for name, data in items:
        f = ("file", (name, io.BytesIO(data), "audio/wav"))
        resp = await server_a_client.upload_training_item(
            files=[f],
            sub_path=sub_path,
            model_version="v2",
            timeout=120.0,
        )
        server_a_client.ensure_success_response(
            resp,
            error_prefix=f"Server A 업로드 실패({name})",
        )
        if first_file is None:
            first_file = name

    return {
        "success": True,
        "data": {
            "train_input_dir": sub_path,
            "first_file": first_file or (items[0][0] if items else ""),
        },
    }


# ============ POST /start ============

MODEL_VERSIONS_WHITELIST = {"v1", "v2", "v4", "v2Pro", "v2ProPlus"}


@router.post("/start")
async def start_training(
    body: ModelMakeStartRequest,
    current_user: User = Depends(get_current_user),
):
    """학습 시작. upload_path = SERVER_A_TRAIN_VOICE_ROOT / train_input_dir 로 Server A /api/train/start 호출."""
    # [왜 여기서 처리하나]
    # 프론트는 train_input_dir만 알고 있으므로 실제 Server A upload_path 조합은 백엔드에서 단일 규칙으로 만든다.
    #
    # [주의]
    # 여기 payload 기본값(batch_size/epoch 등)은 현재 UX와 문서 기준선의 암묵 계약이라 1차에서 바꾸지 않는다.
    if body.version not in MODEL_VERSIONS_WHITELIST:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"지원하는 모델 버전: {', '.join(sorted(MODEL_VERSIONS_WHITELIST))}. 받음: {body.version}",
        )
    root = settings.SERVER_A_TRAIN_VOICE_ROOT
    upload_path = f"{root.rstrip('/')}/{body.train_input_dir.lstrip('/')}"
    payload = {
        "model_name": body.model_name,
        "upload_path": upload_path,
        "version": body.version,
        "batch_size": 11,
        "total_epochs": 8,
        "save_every_epoch": 4,
        "gpu_numbers": "0-0",
        "dry_run": False,
    }
    r = await server_a_client.start_training(payload=payload, timeout=30.0)
    server_a_client.ensure_success_response(r, error_prefix="학습 시작 실패")
    return r.json()


# ============ POST /abort ============

@router.post("/abort")
async def abort_training(
    body: ModelMakeAbortRequest,
    current_user: User = Depends(get_current_user),
):
    """
    모델 제작 중단(트랜잭션 롤백): 업로드 음성(train_input_dir), logs, TEMP 삭제.
    Voice 등록 전 중도 이탈 시 호출. get_current_user 필수.
    """
    # [왜 여기서 처리하나]
    # 모델 제작 플로우 중단 시 "최대한 정리하고 성공 반환"이 UX 계약이라, 삭제 실패 일부는 흡수하고 계속 진행한다.
    #
    # [주의]
    # 존재하지 않는 경로(404)는 실패로 취급하지 않는다. 재시도/중복 클릭을 허용하기 위한 정책이다.
    root_train = settings.SERVER_A_TRAIN_VOICE_ROOT
    root_logs = settings.SERVER_A_LOGS_ROOT
    root_temp = settings.SERVER_A_TEMP_ROOT
    # 1) train_input_dir (업로드 음성 폴더)
    path_train = f"{root_train.rstrip('/')}/{body.train_input_dir.lstrip('/')}"
    try:
        await server_a_client.delete_file(path=path_train, timeout=15.0)
    except HTTPException:
        pass  # 존재하지 않거나 실패해도 계속

    # 2) logs/{model_name}, TEMP/{model_name} (학습 시작한 경우만)
    if body.model_name and body.model_name.strip():
        mn = body.model_name.strip()
        for base, label in [(root_logs, "logs"), (root_temp, "TEMP")]:
            path = f"{base.rstrip('/')}/{mn}"
            try:
                await server_a_client.delete_file(path=path, timeout=15.0)
            except HTTPException:
                pass

    return {"success": True, "message": "중단되었고, 업로드·학습 관련 리소스가 삭제 요청되었습니다."}


# ============ GET /status, /log ============

@router.get("/status/{model_name}")
async def get_training_status(
    model_name: str,
    current_user: User = Depends(get_current_user),
):
    """학습 상태 조회 (Server A /api/train/status/{model_name} 프록시)."""
    r = await server_a_client.get_training_status(model_name=model_name, timeout=10.0)
    server_a_client.ensure_success_response(
        r,
        error_prefix="학습 상태 조회 실패",
        allow_404_detail="학습 상태를 찾을 수 없습니다.",
    )
    return r.json()


@router.get("/log/{model_name}")
async def get_training_log(
    model_name: str,
    current_user: User = Depends(get_current_user),
):
    """학습 로그 조회 (Server A /api/train/log/{model_name} 프록시)."""
    r = await server_a_client.get_training_log(model_name=model_name, timeout=10.0)
    server_a_client.ensure_success_response(
        r,
        error_prefix="학습 로그 조회 실패",
        allow_404_detail="로그를 찾을 수 없습니다.",
    )
    return r.json()


# ============ POST /register ============

@router.post("/register")
async def register_voice(
    body: ModelMakeRegisterRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """학습 완료 후 Voice 등록. ref_audio_path = SERVER_A_TRAIN_VOICE_ROOT/train_input_dir/ref_audio_file. gpt/sovits 미지정 시 GET /api/files/logs에서 model_name 매칭으로 자동 추론."""
    # [왜 여기서 처리하나]
    # /register는 "학습 산출물 -> 서비스 Voice 엔티티"로 넘어가는 경계라서,
    # ref_audio 보정/가중치 자동 추론/DB 등록을 한 곳에서 오케스트레이션한다.
    #
    # [주의]
    # prepare-ref-audio 실패는 전체 실패로 전파하지 않고 원본 ref_audio_file로 계속 진행한다(기존 UX 유지).
    root = settings.SERVER_A_TRAIN_VOICE_ROOT
    
    # 1) ref_audio를 10초 이하로 보정 시도 (실패 시 원본 사용)
    ref_audio_file = await _resolve_ref_audio_file_with_prepare_fallback(
        train_input_dir=body.train_input_dir,
        original_ref_audio_file=body.ref_audio_file,
    )
    
    ref_audio_path = f"{root.rstrip('/')}/{body.train_input_dir.lstrip('/')}/{ref_audio_file}"

    gpt_path = body.gpt_weights_path
    sovits_path = body.sovits_weights_path
    if gpt_path is None and sovits_path is None:
        # 2) 가중치 경로를 명시하지 않은 경우 logs 인덱스에서 model_name으로 역추론한다.
        # 실패해도 Voice 등록은 계속 진행한다(경로 없는 음성도 이후 관리자가 수동 보정 가능).
        try:
            r = await server_a_client.get_files_logs(timeout=5.0)
            if r.status_code == 200:
                data = r.json()
                for m in (data.get("models") or []):
                    if (m.get("model_name") or "").strip() == (body.model_name or "").strip():
                        gpt_path = m.get("gpt_path") or None
                        sovits_path = m.get("sovits_path") or None
                        break
        except Exception:
            pass

    v = Voice(
        id=str(uuid.uuid4()),
        name=body.voice_name,
        description=None,
        language="ko",
        ref_audio_path=ref_audio_path,
        prompt_text="",
        prompt_lang="ko",
        gpt_weights_path=gpt_path,
        sovits_weights_path=sovits_path,
        model_version="v2",
        train_voice_folder=None,
        train_input_dir=body.train_input_dir,
        training_model_name=body.model_name,
        is_default=False,
        is_active=True,
        user_id=current_user.id,
    )
    db.add(v)
    await db.commit()
    await db.refresh(v)
    return {
        "success": True,
        "voice_id": v.id,
        "name": v.name,
        "train_input_dir": v.train_input_dir,
        "training_model_name": v.training_model_name,
    }


# ============ GET /my ============

@router.get("/my")
async def list_my_model_voices(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """내 모델 제작 음성 목록 (user_id=me, train_input_dir·training_model_name 포함)."""
    q = select(Voice).where(Voice.user_id == current_user.id).order_by(Voice.created_at.desc())
    res = await db.execute(q)
    rows = res.scalars().all()
    return {
        "success": True,
        "data": {
            "voices": [
                {
                    "id": v.id,
                    "name": v.name,
                    "train_input_dir": getattr(v, "train_input_dir", None),
                    "training_model_name": getattr(v, "training_model_name", None),
                    "created_at": v.created_at.isoformat() if v.created_at else None,
                }
                for v in rows
            ],
            "total": len(rows),
        },
    }


# ============ DELETE /my/{voice_id} ============

@router.delete("/my/{voice_id}")
async def delete_my_model_voice(
    voice_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    내 모델 제작 음성 삭제.
    DB Voice 삭제, Character voice_id=NULL, Server A train_input_dir 폴더·logs/{training_model_name} 삭제.
    """
    # [왜 여기서 처리하나]
    # 사용자 자산 정리 경로라 DB/캐릭터 연결/Server A 파일 삭제를 같은 요청에서 묶어 "남은 찌꺼기"를 줄인다.
    #
    # [주의]
    # Server A 삭제 실패(HTTPException)는 흡수하고 DB 삭제를 계속 진행한다. 1차 정책은 사용자 경험 일관성 우선이다.
    res = await db.execute(select(Voice).where(Voice.id == voice_id, Voice.user_id == current_user.id))
    v = res.scalar_one_or_none()
    if not v:
        raise HTTPException(status_code=404, detail="음성을 찾을 수 없습니다.")

    # 연결 캐릭터 해제
    await db.execute(Character.__table__.update().where(Character.voice_id == voice_id).values(voice_id=None))

    train_dir = getattr(v, "train_input_dir", None)
    model_name = getattr(v, "training_model_name", None)
    root_train = settings.SERVER_A_TRAIN_VOICE_ROOT
    root_logs = settings.SERVER_A_LOGS_ROOT
    if train_dir:
        path = f"{root_train.rstrip('/')}/{train_dir.lstrip('/')}"
        try:
            await server_a_client.delete_file(path=path, timeout=15.0)
        except HTTPException:
            pass
    if model_name:
        path = f"{root_logs.rstrip('/')}/{model_name}"
        try:
            await server_a_client.delete_file(path=path, timeout=15.0)
        except HTTPException:
            pass

    await db.delete(v)
    await db.commit()
    return {"success": True, "message": f"음성 '{v.name}'과 관련 리소스가 삭제되었습니다."}
