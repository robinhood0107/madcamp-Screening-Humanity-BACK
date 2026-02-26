# GET /api/users/me/settings, PUT /api/users/me/settings
from fastapi import APIRouter, Depends
from typing import Any, Dict
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.services import user_preferences_data_gateway

router = APIRouter()

# 설정에 허용할 키 (부분 업데이트 시 이 키만 반영)
ALLOWED_SETTINGS_KEYS = {"tts_mode", "tts_delay_ms", "tts_streaming_mode", "tts_enabled", "tts_speed"}


@router.get("/me/settings")
async def get_my_settings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """로그인 사용자의 settings JSON 반환. 없으면 {}."""
    out = await user_preferences_data_gateway.get_settings_for_user(db=db, user_id=current_user.id)
    return {"success": True, "data": out}


@router.put("/me/settings")
async def put_my_settings(
    body: Dict[str, Any],  # {"tts_speed": 1.2, ...} 부분 업데이트
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """body에 포함된 키만 기존 settings에 병합. 허용 키: tts_mode, tts_delay_ms, tts_streaming_mode, tts_enabled, tts_speed."""
    to_merge = {k: v for k, v in body.items() if k in ALLOWED_SETTINGS_KEYS}
    if not to_merge:
        cur = await user_preferences_data_gateway.get_settings_for_user(db=db, user_id=current_user.id)
        return {"success": True, "data": cur}
    merged = await user_preferences_data_gateway.put_settings_for_user(
        db=db,
        user_id=current_user.id,
        settings_patch=to_merge,
    )
    return {"success": True, "data": merged}
