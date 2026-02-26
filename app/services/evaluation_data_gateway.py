from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.conversation import Conversation
from app.services.data_service_client import data_service_client

logger = logging.getLogger(__name__)
_fallback_warned_ops: set[str] = set()
_success_logged_ops: set[str] = set()


def _warn_fallback_once(op_name: str, detail: Dict[str, Any] | None = None) -> None:
    if op_name in _fallback_warned_ops:
        return
    _fallback_warned_ops.add(op_name)
    logger.warning(
        "USE_C_DATA_SERVICE rehearsal fallback for %s -> SQLAlchemy/text evaluations (detail=%s)",
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


async def create_evaluation_for_legacy_session(
    *,
    db: AsyncSession,
    legacy_session_id: str,
    score: Optional[int],
    summary: str,
    feedback: str,
    raw_payload: Dict[str, Any] | None,
) -> Dict[str, Any]:
    grpc_result = _rehearsal_call(
        "evaluation_create_by_session",
        "evaluation_create_by_session",
        {
            "legacy_session_id": legacy_session_id,
            "score": score,
            "summary": summary,
            "feedback": feedback,
            "raw_payload": raw_payload or {},
        },
    )
    if isinstance(grpc_result, dict):
        if grpc_result.get("created"):
            return grpc_result
        # data-service가 명시적으로 conversation 없음이라면 fallback에서도 결과 동일.
        if grpc_result.get("reason") == "conversation_not_found":
            return grpc_result

    result = await db.execute(
        select(Conversation).where(Conversation.legacy_session_id == legacy_session_id)
    )
    conv = result.scalar_one_or_none()
    if not conv:
        return {
            "created": False,
            "reason": "conversation_not_found",
            "evaluation_id": None,
            "conversation_id": None,
            "created_at": None,
        }

    evaluation_id = str(uuid.uuid4())
    try:
        await db.execute(
            text(
                """
                INSERT INTO evaluations (id, conversation_id, score, feedback, summary, raw_payload)
                VALUES (:id, :conversation_id, :score, :feedback, :summary, :raw_payload)
                """
            ),
            {
                "id": evaluation_id,
                "conversation_id": conv.id,
                "score": score,
                "feedback": feedback,
                "summary": summary,
                "raw_payload": json.dumps(raw_payload or {}, ensure_ascii=False),
            },
        )
        await db.commit()
        return {
            "created": True,
            "evaluation_id": evaluation_id,
            "conversation_id": conv.id,
            "created_at": None,
            "reason": None,
        }
    except Exception as exc:
        try:
            await db.rollback()
        except Exception:
            logger.exception("Failed to rollback evaluation insert fallback")
        logger.warning(
            "Evaluation persistence fallback insert failed session_id=%s conversation_id=%s err=%s",
            legacy_session_id,
            getattr(conv, "id", None),
            exc,
        )
        return {
            "created": False,
            "reason": "insert_failed",
            "evaluation_id": None,
            "conversation_id": conv.id,
            "created_at": None,
        }

