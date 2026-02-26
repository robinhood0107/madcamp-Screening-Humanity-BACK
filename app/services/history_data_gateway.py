from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthPrincipal
from app.core.config import settings
from app.services import history_service
from app.services.data_service_client import data_service_client

logger = logging.getLogger(__name__)
_fallback_warned_ops: set[str] = set()
_success_logged_ops: set[str] = set()


def _warn_fallback_once(op_name: str, detail: Optional[dict[str, Any]] = None) -> None:
    if op_name in _fallback_warned_ops:
        return
    _fallback_warned_ops.add(op_name)
    logger.warning(
        "USE_C_DATA_SERVICE rehearsal fallback for %s -> Python history_service (detail=%s)",
        op_name,
        detail or {},
    )


def _log_success_once(op_name: str, detail: Optional[dict[str, Any]] = None) -> None:
    if op_name in _success_logged_ops:
        return
    _success_logged_ops.add(op_name)
    logger.info(
        "USE_C_DATA_SERVICE rehearsal path received gRPC response for %s (detail=%s)",
        op_name,
        detail or {},
    )


def _owner_ids_from_principal(principal: AuthPrincipal) -> dict[str, Optional[str]]:
    if principal.kind == "user" and principal.user:
        return {"owner_user_id": principal.user.id, "owner_guest_session_id": None}
    if principal.kind == "guest" and principal.guest_session:
        return {"owner_user_id": None, "owner_guest_session_id": principal.guest_session.id}
    return {"owner_user_id": None, "owner_guest_session_id": None}


def _rehearsal_call(op_name: str, payload: dict[str, Any], client_method_name: str) -> Optional[dict[str, Any]]:
    if not data_service_client.is_enabled():
        return None
    client_method = getattr(data_service_client, client_method_name)
    try:
        result = client_method(payload=payload)
        _log_success_once(op_name, {"grpc_addr": data_service_client.cfg.grpc_addr, "status": "call_ok"})
        return result
    except (NotImplementedError, RuntimeError, ValueError) as exc:
        health = data_service_client.health_check()
        _warn_fallback_once(
            op_name,
            {
                "grpc_addr": health.get("grpc_addr"),
                "status": health.get("status"),
                "error": str(exc),
            },
        )
        if data_service_client.is_enabled() and not settings.DATA_SERVICE_REHEARSAL_FALLBACK_ENABLED:
            raise RuntimeError(
                f"gRPC rehearsal call failed for {op_name} and fallback is disabled: {exc}"
            ) from exc
        return None


async def list_history_items(
    *,
    db: AsyncSession,
    principal: AuthPrincipal,
    status_filter: Optional[str],
    mode: Optional[str],
    has_audio: Optional[bool],
    limit: int,
    search: Optional[str],
) -> dict[str, Any]:
    owner_ids = _owner_ids_from_principal(principal)
    grpc_result = _rehearsal_call(
        "list_history_items",
        {
            "status_filter": status_filter,
            "mode": mode,
            "has_audio": has_audio,
            "limit": limit,
            "search": search,
            "principal_kind": getattr(principal, "kind", None),
            **owner_ids,
        },
        "history_list_conversations",
    )
    if isinstance(grpc_result, dict) and "items" in grpc_result:
        return grpc_result
    return await history_service.list_history_items(
        db=db,
        principal=principal,
        status_filter=status_filter,
        mode=mode,
        has_audio=has_audio,
        limit=limit,
        search=search,
    )


async def get_history_detail_for_owner(
    *,
    db: AsyncSession,
    principal: AuthPrincipal,
    conversation_id: str,
) -> Optional[dict[str, Any]]:
    owner_ids = _owner_ids_from_principal(principal)
    grpc_result = _rehearsal_call(
        "get_history_detail_for_owner",
        {
            "conversation_id": conversation_id,
            "principal_kind": getattr(principal, "kind", None),
            **owner_ids,
        },
        "history_get_detail",
    )
    if isinstance(grpc_result, dict) and {"conversation", "messages", "audio_assets"}.issubset(grpc_result.keys()):
        return grpc_result
    return await history_service.get_history_detail_for_owner(
        db=db,
        principal=principal,
        conversation_id=conversation_id,
    )


async def soft_delete_history_for_owner(
    *,
    db: AsyncSession,
    principal: AuthPrincipal,
    conversation_id: str,
) -> Optional[dict[str, Any]]:
    owner_ids = _owner_ids_from_principal(principal)
    grpc_result = _rehearsal_call(
        "soft_delete_history_for_owner",
        {
            "conversation_id": conversation_id,
            "principal_kind": getattr(principal, "kind", None),
            **owner_ids,
        },
        "history_soft_delete",
    )
    if isinstance(grpc_result, dict) and grpc_result.get("conversation_id"):
        return grpc_result
    return await history_service.soft_delete_history_for_owner(
        db=db,
        principal=principal,
        conversation_id=conversation_id,
    )


async def guest_merge_preview_counts(
    *,
    db: AsyncSession,
    guest_session_id: str,
) -> dict[str, int]:
    grpc_result = _rehearsal_call(
        "guest_merge_preview_counts",
        {"guest_session_id": guest_session_id},
        "guest_merge_preview_counts",
    )
    if isinstance(grpc_result, dict) and {"conversation_count", "audio_count", "storage_bytes"}.issubset(grpc_result.keys()):
        return {
            "conversation_count": int(grpc_result["conversation_count"]),
            "audio_count": int(grpc_result["audio_count"]),
            "storage_bytes": int(grpc_result["storage_bytes"]),
        }
    return await history_service.guest_merge_preview_counts(
        db=db,
        guest_session_id=guest_session_id,
    )


async def move_guest_history_to_user(
    *,
    db: AsyncSession,
    guest_session_id: str,
    user_id: str,
) -> dict[str, int]:
    grpc_result = _rehearsal_call(
        "move_guest_history_to_user",
        {"guest_session_id": guest_session_id, "user_id": user_id},
        "guest_merge_move_to_user",
    )
    if isinstance(grpc_result, dict) and {"conversation_count", "audio_count"}.issubset(grpc_result.keys()):
        return {
            "conversation_count": int(grpc_result["conversation_count"]),
            "audio_count": int(grpc_result["audio_count"]),
        }
    return await history_service.move_guest_history_to_user(
        db=db,
        guest_session_id=guest_session_id,
        user_id=user_id,
    )
