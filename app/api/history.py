import io
from pathlib import Path
from typing import Optional
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthPrincipal, get_current_principal_optional
from app.core.database import get_db
from app.services import chat_service, history_data_gateway, history_service

router = APIRouter()


class HistoryListQuery(BaseModel):
    status: Optional[str] = None
    mode: Optional[str] = None
    has_audio: Optional[bool] = None
    cursor: Optional[str] = None
    limit: int = 20
    search: Optional[str] = None


class HistoryAudioDownloadRequest(BaseModel):
    audio_ids: Optional[list[str]] = None
    message_ids: Optional[list[str]] = None


class HistoryTtsRegenerateRequest(BaseModel):
    message_id: Optional[str] = None
    text: Optional[str] = Field(default=None, max_length=10000)
    voice_id: Optional[str] = None


def _require_any_principal(principal: Optional[AuthPrincipal]) -> AuthPrincipal:
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="인증이 필요합니다",
        )
    return principal


def _build_audio_download_name(conversation_title: Optional[str], suffix: str) -> str:
    raw = (conversation_title or "conversation_audio").strip()
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in raw)[:60].strip("_")
    return f"{safe or 'conversation_audio'}{suffix}"


def _build_zip_bytes(file_items: list[dict]) -> bytes:
    buf = io.BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, item in enumerate(file_items, start=1):
            asset = item["asset"]
            path: Path = item["path"]
            ext = path.suffix or ".bin"
            base_name = f"{idx:03d}_{asset.id[:8]}{ext}"
            name = base_name
            serial = 2
            while name in used_names:
                name = f"{idx:03d}_{asset.id[:8]}_{serial}{ext}"
                serial += 1
            used_names.add(name)
            zf.write(path, arcname=name)
    return buf.getvalue()


@router.get("/history")
async def list_history(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    mode: Optional[str] = Query(default=None),
    has_audio: Optional[bool] = Query(default=None),
    cursor: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    search: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    principal: Optional[AuthPrincipal] = Depends(get_current_principal_optional),
):
    principal = _require_any_principal(principal)
    _ = HistoryListQuery(
        status=status_filter,
        mode=mode,
        has_audio=has_audio,
        cursor=cursor,
        limit=limit,
        search=search,
    )
    data = await history_data_gateway.list_history_items(
        db=db,
        principal=principal,
        status_filter=status_filter,
        mode=mode,
        has_audio=has_audio,
        limit=limit,
        search=search,
    )
    return {"success": True, "data": data}


@router.get("/history/{conversation_id}")
async def get_history_detail(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    principal: Optional[AuthPrincipal] = Depends(get_current_principal_optional),
):
    principal = _require_any_principal(principal)
    data = await history_data_gateway.get_history_detail_for_owner(
        db=db,
        principal=principal,
        conversation_id=conversation_id,
    )
    if data is None:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    return {"success": True, "data": data}


@router.delete("/history/{conversation_id}")
async def delete_history(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    principal: Optional[AuthPrincipal] = Depends(get_current_principal_optional),
):
    principal = _require_any_principal(principal)
    data = await history_data_gateway.soft_delete_history_for_owner(
        db=db,
        principal=principal,
        conversation_id=conversation_id,
    )
    if data is None:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")
    return {"success": True, "data": data}


@router.post("/history/{conversation_id}/audio/download")
async def download_history_audio(
    conversation_id: str,
    body: HistoryAudioDownloadRequest,
    db: AsyncSession = Depends(get_db),
    principal: Optional[AuthPrincipal] = Depends(get_current_principal_optional),
):
    principal = _require_any_principal(principal)
    result = await history_service.list_history_audio_assets_for_owner(
        db=db,
        principal=principal,
        conversation_id=conversation_id,
        audio_ids=body.audio_ids,
        message_ids=body.message_ids,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")

    files = result["files"]
    conversation = result["conversation"]
    if not files:
        raise HTTPException(status_code=404, detail="다운로드 가능한 오디오가 없습니다.")

    if len(files) == 1:
        file_item = files[0]
        path: Path = file_item["path"]
        media_type = file_item["asset"].mime_type or "application/octet-stream"
        filename = _build_audio_download_name(conversation.title, path.suffix or ".bin")
        return FileResponse(path=str(path), media_type=media_type, filename=filename)

    zip_bytes = _build_zip_bytes(files)
    filename = _build_audio_download_name(conversation.title, ".zip")
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/history/{conversation_id}/audio/download-all")
async def download_history_audio_all(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    principal: Optional[AuthPrincipal] = Depends(get_current_principal_optional),
):
    principal = _require_any_principal(principal)
    result = await history_service.list_history_audio_assets_for_owner(
        db=db,
        principal=principal,
        conversation_id=conversation_id,
        audio_ids=None,
        message_ids=None,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="기록을 찾을 수 없습니다.")

    files = result["files"]
    conversation = result["conversation"]
    if not files:
        raise HTTPException(status_code=404, detail="다운로드 가능한 오디오가 없습니다.")

    zip_bytes = _build_zip_bytes(files)
    filename = _build_audio_download_name(conversation.title, ".zip")
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/history/{conversation_id}/tts/regenerate")
async def regenerate_history_tts(
    conversation_id: str,
    body: HistoryTtsRegenerateRequest,
    db: AsyncSession = Depends(get_db),
    principal: Optional[AuthPrincipal] = Depends(get_current_principal_optional),
):
    principal = _require_any_principal(principal)
    try:
        target = await history_service.resolve_history_tts_regenerate_target(
            db=db,
            principal=principal,
            conversation_id=conversation_id,
            message_id=body.message_id,
            text=body.text,
            voice_id=body.voice_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="기록을 찾을 수 없습니다.")

    current_user = principal.user if principal.kind == "user" else None
    tts_data = await chat_service.synthesize_chat_tts_result(
        text=target["text"],
        voice_id=target.get("voice_id"),
        streaming_mode=0,
        speed_factor=1.0,
        current_user=current_user,
        db=db,
    )
    if not tts_data:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="TTS 생성에 실패했습니다.")

    linked_asset = await history_service.attach_conversation_audio_asset_from_chat_tts(
        db=db,
        principal=principal,
        conversation_id=target["conversation"].id,
        message_id=target.get("message_id"),
        tts_data=tts_data,
        fallback_voice_id=target.get("voice_id"),
    )

    return {
        "success": True,
        "data": {
            "conversation_id": target["conversation"].id,
            "message_id": target.get("message_id"),
            "requested_text": target["text"],
            "tts": tts_data,
            "audio_asset": (
                {
                    "id": linked_asset.id,
                    "voice_id": linked_asset.voice_id,
                    "duration_sec": linked_asset.duration_sec,
                    "download_url": linked_asset.file_url,
                    "play_url": linked_asset.file_url,
                    "mime_type": linked_asset.mime_type,
                    "size_bytes": linked_asset.size_bytes,
                }
                if linked_asset
                else None
            ),
        },
    }
