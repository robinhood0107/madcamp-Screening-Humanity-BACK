"""
[역할]
인증 감사 로그(AuthEvent) 생성 공통 helper.

[주의]
- 감사 로그 실패가 로그인/로그아웃 자체를 막지 않도록 best-effort로 호출하는 것이 기본 정책이다.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth_event import AuthEvent
from app.services.data_service_client import data_service_client

logger = logging.getLogger(__name__)


def _extract_client_ip(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


async def log_auth_event(
    db: AsyncSession,
    *,
    request: Optional[Request],
    actor_type: str,
    actor_id: Optional[str],
    provider: str,
    event_type: str,
    success: bool,
    reason: Optional[str] = None,
    commit: bool = False,
) -> None:
    client_ip = _extract_client_ip(request)
    user_agent = (request.headers.get("user-agent") if request else None)

    if data_service_client.is_enabled():
        try:
            data_service_client.auth_event_create(
                payload={
                    "actor_type": actor_type,
                    "actor_id": actor_id,
                    "provider": provider,
                    "event_type": event_type,
                    "success": success,
                    "reason": reason,
                    "client_ip": client_ip,
                    "user_agent": user_agent,
                }
            )
            return
        except Exception:
            logger.warning(
                "Auth audit data-service path failed; falling back to SQLAlchemy actor_type=%s actor_id=%s provider=%s event_type=%s",
                actor_type,
                actor_id,
                provider,
                event_type,
                exc_info=True,
            )

    try:
        db.add(
            AuthEvent(
                actor_type=actor_type,
                actor_id=actor_id,
                provider=provider,
                event_type=event_type,
                success=success,
                reason=reason,
                client_ip=client_ip,
                user_agent=user_agent,
            )
        )
        if commit:
            await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            logger.exception("Failed to rollback auth audit DB session after logging failure")
        logger.exception(
            "Failed to write auth audit event actor_type=%s actor_id=%s provider=%s event_type=%s",
            actor_type,
            actor_id,
            provider,
            event_type,
        )
