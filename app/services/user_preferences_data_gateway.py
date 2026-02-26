from __future__ import annotations

import logging
from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.user_preference import UserPreference
from app.services.data_service_client import data_service_client

logger = logging.getLogger(__name__)
_fallback_warned_ops: set[str] = set()
_success_logged_ops: set[str] = set()


def _warn_fallback_once(op_name: str, detail: Dict[str, Any] | None = None) -> None:
    if op_name in _fallback_warned_ops:
        return
    _fallback_warned_ops.add(op_name)
    logger.warning(
        "USE_C_DATA_SERVICE rehearsal fallback for %s -> SQLAlchemy user_preferences (detail=%s)",
        op_name,
        detail or {},
    )


def _log_success_once(op_name: str, detail: Dict[str, Any] | None = None) -> None:
    if op_name in _success_logged_ops:
        return
    _success_logged_ops.add(op_name)
    logger.info(
        "USE_C_DATA_SERVICE rehearsal path received data-service response for %s (detail=%s)",
        op_name,
        detail or {},
    )


def _rehearsal_call(op_name: str, client_method_name: str, payload: Dict[str, Any]) -> Dict[str, Any] | None:
    if not data_service_client.is_enabled():
        return None
    client_method = getattr(data_service_client, client_method_name)
    try:
        result = client_method(payload=payload)
        _log_success_once(op_name, {"status": "call_ok", "grpc_addr": data_service_client.cfg.grpc_addr})
        return result
    except (NotImplementedError, RuntimeError, ValueError) as exc:
        _warn_fallback_once(
            op_name,
            {
                "error": str(exc),
                "grpc_addr": data_service_client.cfg.grpc_addr,
            },
        )
        if data_service_client.is_enabled() and not settings.DATA_SERVICE_REHEARSAL_FALLBACK_ENABLED:
            raise RuntimeError(
                f"data-service rehearsal call failed for {op_name} and fallback is disabled: {exc}"
            ) from exc
        return None


async def get_settings_for_user(*, db: AsyncSession, user_id: str) -> Dict[str, Any]:
    grpc_result = _rehearsal_call(
        "user_preferences_get",
        "user_preferences_get",
        {"user_id": user_id},
    )
    if isinstance(grpc_result, dict) and isinstance(grpc_result.get("settings"), dict):
        return grpc_result["settings"] or {}

    r = await db.execute(select(UserPreference).where(UserPreference.user_id == user_id))
    row = r.scalar_one_or_none()
    if not row:
        return {}
    return row.settings if isinstance(row.settings, dict) else {}


async def put_settings_for_user(*, db: AsyncSession, user_id: str, settings_patch: Dict[str, Any]) -> Dict[str, Any]:
    grpc_result = _rehearsal_call(
        "user_preferences_put",
        "user_preferences_put",
        {"user_id": user_id, "settings_patch": settings_patch},
    )
    if isinstance(grpc_result, dict) and isinstance(grpc_result.get("settings"), dict):
        return grpc_result["settings"] or {}

    r = await db.execute(select(UserPreference).where(UserPreference.user_id == user_id))
    row = r.scalar_one_or_none()
    if not row:
        pref = UserPreference(user_id=user_id, settings=dict(settings_patch))
        db.add(pref)
        await db.commit()
        await db.refresh(pref)
        return pref.settings or settings_patch

    cur = (row.settings if isinstance(row.settings, dict) else {}) or {}
    cur.update(settings_patch)
    row.settings = cur
    await db.commit()
    await db.refresh(row)
    return row.settings or cur

