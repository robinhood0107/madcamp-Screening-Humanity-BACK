from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services import character_catalog_service
from app.services.data_service_client import data_service_client

logger = logging.getLogger(__name__)
_fallback_warned_ops: set[str] = set()
_success_logged_ops: set[str] = set()


def _warn_fallback_once(op_name: str, detail: dict[str, Any] | None = None) -> None:
    if op_name in _fallback_warned_ops:
        return
    _fallback_warned_ops.add(op_name)
    logger.warning(
        "USE_C_DATA_SERVICE rehearsal fallback for %s -> character_catalog_service (detail=%s)",
        op_name,
        detail or {},
    )


def _log_success_once(op_name: str, detail: dict[str, Any] | None = None) -> None:
    if op_name in _success_logged_ops:
        return
    _success_logged_ops.add(op_name)
    logger.info(
        "USE_C_DATA_SERVICE rehearsal path received data-service response for %s (detail=%s)",
        op_name,
        detail or {},
    )


def _rehearsal_call(op_name: str, client_method_name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
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
            {"error": str(exc), "grpc_addr": data_service_client.cfg.grpc_addr},
        )
        if data_service_client.is_enabled() and not settings.DATA_SERVICE_REHEARSAL_FALLBACK_ENABLED:
            raise RuntimeError(
                f"data-service rehearsal call failed for {op_name} and fallback is disabled: {exc}"
            ) from exc
        return None


async def list_public_preset_items_as_legacy_payload(*, db: AsyncSession, limit: int = 500):
    grpc_result = _rehearsal_call(
        "character_catalog_list_public_presets_legacy",
        "character_catalog_list_public_presets_legacy",
        {"limit": limit},
    )
    if isinstance(grpc_result, dict) and isinstance(grpc_result.get("items"), list):
        return grpc_result["items"]
    try:
        return await character_catalog_service.list_public_preset_items_as_legacy_payload(db=db, limit=limit)
    except SQLAlchemyError:
        raise

