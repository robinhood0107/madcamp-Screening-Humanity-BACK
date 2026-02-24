"""
[역할]
로그인/로그아웃/인증 실패 등 인증 관련 감사 이벤트를 기록한다.
"""

from sqlalchemy import Column, String, DateTime, Boolean, Index
from sqlalchemy.sql import func
import uuid

from app.core.database import Base


class AuthEvent(Base):
    __tablename__ = "auth_events"

    id = Column(String(36), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    actor_type = Column(String(32), nullable=False, index=True)  # google_user | guest
    actor_id = Column(String(64), nullable=True, index=True)
    event_type = Column(String(32), nullable=False)  # login | logout | auth_me | token_invalid | expired
    provider = Column(String(20), nullable=False)  # google | guest
    success = Column(Boolean, nullable=False, default=True)
    reason = Column(String(255), nullable=True)
    client_ip = Column(String(128), nullable=True)
    user_agent = Column(String(512), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    __table_args__ = (
        Index("ix_auth_events_actor_type_actor_id", "actor_type", "actor_id"),
    )

