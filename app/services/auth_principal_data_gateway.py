from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.guest_session import GuestSession
from app.models.user import User
from app.services.data_service_client import data_service_client

logger = logging.getLogger(__name__)
_fallback_warned_ops: set[str] = set()
_success_logged_ops: set[str] = set()


def _warn_fallback_once(op_name: str, detail: Dict[str, Any] | None = None) -> None:
    if op_name in _fallback_warned_ops:
        return
    _fallback_warned_ops.add(op_name)
    logger.warning(
        "USE_C_DATA_SERVICE rehearsal fallback for %s -> SQLAlchemy guest_sessions (detail=%s)",
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
            {"error": str(exc), "grpc_addr": data_service_client.cfg.grpc_addr},
        )
        if data_service_client.is_enabled() and not settings.DATA_SERVICE_REHEARSAL_FALLBACK_ENABLED:
            raise RuntimeError(
                f"data-service rehearsal call failed for {op_name} and fallback is disabled: {exc}"
            ) from exc
        return None


def _parse_dt(raw: Any) -> Optional[datetime]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        # postgres text cast may emit +00 or Z
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None
    return None


def _guest_model_from_row(row: Dict[str, Any]) -> GuestSession:
    guest = GuestSession(
        id=str(row.get("id")),
        display_name=row.get("display_name"),
        status=row.get("status") or "active",
    )
    guest.created_at = _parse_dt(row.get("created_at"))
    guest.expires_at = _parse_dt(row.get("expires_at"))
    if row.get("merged_to_user_id") is not None:
        # SQLAlchemy model currently has no merged_to_user_id field; keep as dynamic attr for convenience.
        setattr(guest, "merged_to_user_id", row.get("merged_to_user_id"))
    return guest


def _user_model_from_row(row: Dict[str, Any]) -> User:
    user = User(
        id=str(row.get("id")),
        email=row.get("email") or "",
        username=row.get("username"),
        picture=row.get("picture"),
        provider=row.get("provider") or "google",
    )
    if row.get("is_active") is not None:
        user.is_active = bool(row.get("is_active"))
    if row.get("is_superuser") is not None:
        user.is_superuser = bool(row.get("is_superuser"))
    user.created_at = _parse_dt(row.get("created_at"))
    return user


async def create_guest_session(
    *,
    db: AsyncSession,
    guest_session_id: str,
    display_name: str,
    expires_at: Optional[datetime],
    client_ip: Optional[str],
    user_agent: Optional[str],
) -> GuestSession:
    grpc_result = _rehearsal_call(
        "guest_session_create",
        "guest_session_create",
        {
            "guest_session_id": guest_session_id,
            "display_name": display_name,
            "expires_at": expires_at.isoformat() if expires_at else "",
            "client_ip": client_ip or "",
            "user_agent": user_agent or "",
        },
    )
    if isinstance(grpc_result, dict) and grpc_result.get("found") and isinstance(grpc_result.get("guest_session"), dict):
        return _guest_model_from_row(grpc_result["guest_session"])

    now = datetime.utcnow()
    guest_session = GuestSession(
        id=guest_session_id,
        display_name=display_name,
        status="active",
        last_seen_at=now,
        expires_at=expires_at,
        client_ip=client_ip,
        user_agent=user_agent,
    )
    db.add(guest_session)
    await db.commit()
    await db.refresh(guest_session)
    return guest_session


async def get_user_by_id(*, db: AsyncSession, user_id: str) -> Optional[User]:
    grpc_result = _rehearsal_call(
        "user_get_by_id",
        "user_get_by_id",
        {"user_id": user_id},
    )
    if isinstance(grpc_result, dict) and grpc_result.get("found") and isinstance(grpc_result.get("user"), dict):
        return _user_model_from_row(grpc_result["user"])
    if isinstance(grpc_result, dict) and grpc_result.get("found") is False:
        return None

    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_guest_session_by_id(*, db: AsyncSession, guest_session_id: str) -> Optional[GuestSession]:
    grpc_result = _rehearsal_call(
        "guest_session_get",
        "guest_session_get",
        {"guest_session_id": guest_session_id},
    )
    if isinstance(grpc_result, dict) and grpc_result.get("found") and isinstance(grpc_result.get("guest_session"), dict):
        return _guest_model_from_row(grpc_result["guest_session"])
    if isinstance(grpc_result, dict) and grpc_result.get("found") is False:
        return None

    result = await db.execute(select(GuestSession).where(GuestSession.id == guest_session_id))
    return result.scalar_one_or_none()


async def update_guest_session_status(
    *,
    db: AsyncSession,
    guest_session_id: str,
    status_value: str,
    reason: Optional[str] = None,
) -> Optional[GuestSession]:
    grpc_result = _rehearsal_call(
        "guest_session_update_status",
        "guest_session_update_status",
        {"guest_session_id": guest_session_id, "status": status_value, "reason": reason or ""},
    )
    if isinstance(grpc_result, dict) and grpc_result.get("found") and isinstance(grpc_result.get("guest_session"), dict):
        return _guest_model_from_row(grpc_result["guest_session"])
    if isinstance(grpc_result, dict) and grpc_result.get("found") is False:
        return None

    result = await db.execute(select(GuestSession).where(GuestSession.id == guest_session_id))
    guest = result.scalar_one_or_none()
    if not guest:
        return None
    guest.status = status_value
    guest.last_seen_at = datetime.utcnow()
    if status_value in {"logged_out", "expired", "merged", "revoked"} and not guest.ended_at:
        guest.ended_at = datetime.utcnow()
    await db.commit()
    await db.refresh(guest)
    return guest


async def touch_guest_session(*, db: AsyncSession, guest_session_id: str) -> Optional[GuestSession]:
    return await update_guest_session_status(
        db=db,
        guest_session_id=guest_session_id,
        status_value="active",
        reason="touch",
    )
