from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.summary import ChatSummary
from app.services.data_service_client import data_service_client

logger = logging.getLogger(__name__)
_fallback_warned_ops: set[str] = set()
_success_logged_ops: set[str] = set()


def _warn_fallback_once(op_name: str, detail: Dict[str, Any] | None = None) -> None:
    if op_name in _fallback_warned_ops:
        return
    _fallback_warned_ops.add(op_name)
    logger.warning(
        "USE_C_DATA_SERVICE rehearsal fallback for %s -> SQLAlchemy chat_summaries (detail=%s)",
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


async def get_summary_for_session(*, db: AsyncSession, session_id: str) -> Optional[str]:
    grpc_result = _rehearsal_call(
        "chat_summary_get_by_session",
        "chat_summary_get_by_session",
        {"session_id": session_id},
    )
    if isinstance(grpc_result, dict):
        if grpc_result.get("found") is False:
            return None
        summary = grpc_result.get("summary")
        if isinstance(summary, str) and summary:
            return summary
        if isinstance(summary, str):
            return None

    result = await db.execute(select(ChatSummary).where(ChatSummary.session_id == session_id))
    row = result.scalar_one_or_none()
    if row and row.summary:
        return row.summary
    return None


async def put_summary_for_session(*, db: AsyncSession, session_id: str, summary: str) -> None:
    grpc_result = _rehearsal_call(
        "chat_summary_put_by_session",
        "chat_summary_put_by_session",
        {"session_id": session_id, "summary": summary},
    )
    if isinstance(grpc_result, dict):
        return

    result = await db.execute(select(ChatSummary).where(ChatSummary.session_id == session_id))
    row = result.scalar_one_or_none()
    if row:
        row.summary = summary
    else:
        db.add(ChatSummary(session_id=session_id, summary=summary))
    await db.commit()

