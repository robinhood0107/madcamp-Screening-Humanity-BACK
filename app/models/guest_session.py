"""
[역할]
게스트 인증 세션의 서버 측 식별/수명주기를 저장한다.

[주의]
- guest는 users 테이블에 넣지 않고 별도 세션 엔티티로 관리한다.
- expires_at/status 기반으로 만료/정리 정책을 운영에서 분리할 수 있게 설계했다.
"""

from sqlalchemy import Column, String, DateTime, Index
from sqlalchemy.sql import func
import uuid

from app.core.database import Base


class GuestSession(Base):
    __tablename__ = "guest_sessions"

    id = Column(String(36), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    display_name = Column(String(80), nullable=True)
    status = Column(String(20), nullable=False, default="active", index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    client_ip = Column(String(128), nullable=True)
    user_agent = Column(String(512), nullable=True)

    __table_args__ = (
        Index("ix_guest_sessions_status_expires_at", "status", "expires_at"),
    )

