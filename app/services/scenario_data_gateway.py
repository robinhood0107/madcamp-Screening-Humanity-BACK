from __future__ import annotations

import logging
from typing import Any, Dict

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.scenario import Scenario
from app.services.data_service_client import data_service_client

logger = logging.getLogger(__name__)
_fallback_warned_ops: set[str] = set()
_success_logged_ops: set[str] = set()


def _warn_fallback_once(op_name: str, detail: Dict[str, Any] | None = None) -> None:
    if op_name in _fallback_warned_ops:
        return
    _fallback_warned_ops.add(op_name)
    logger.warning(
        "USE_C_DATA_SERVICE rehearsal fallback for %s -> SQLAlchemy scenarios (detail=%s)",
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
        _warn_fallback_once(op_name, {"error": str(exc), "grpc_addr": data_service_client.cfg.grpc_addr})
        if data_service_client.is_enabled() and not settings.DATA_SERVICE_REHEARSAL_FALLBACK_ENABLED:
            raise RuntimeError(
                f"data-service rehearsal call failed for {op_name} and fallback is disabled: {exc}"
            ) from exc
        return None


async def create_scenario_for_user(
    *,
    db: AsyncSession,
    user_id: str,
    user_name: str,
    character_name: str,
    situation: str,
    summary: str,
    background: str,
) -> Dict[str, Any]:
    grpc_result = _rehearsal_call(
        "scenario_create_user",
        "scenario_create_user",
        {
            "user_id": user_id,
            "user_name": user_name,
            "character_name": character_name,
            "situation": situation,
            "summary": summary,
            "background": background,
        },
    )
    if isinstance(grpc_result, dict) and grpc_result.get("scenario_id"):
        return {
            "scenario_id": grpc_result["scenario_id"],
            "summary": grpc_result.get("summary") or summary,
            "background": grpc_result.get("background") or background,
        }

    new_scenario = Scenario(
        user_id=user_id,
        user_name=user_name,
        character_name=character_name,
        situation=situation,
        summary=summary,
        background=background,
    )
    db.add(new_scenario)
    await db.commit()
    await db.refresh(new_scenario)
    return {
        "scenario_id": new_scenario.id,
        "summary": new_scenario.summary or summary,
        "background": new_scenario.background or background,
    }
