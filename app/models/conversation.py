"""
[역할]
History API(`/api/history*`)의 대화 루트 엔티티를 저장한다.

[주의]
- 기존 `chat_messages.session_id`는 레거시 호환 키로 유지하고, 이 테이블의 `legacy_session_id`와 매핑한다.
- 소유권은 user 또는 guest_session 중 정확히 하나만 갖는 것을 애플리케이션 레벨에서 보장한다.
"""

from sqlalchemy import Column, String, Text, DateTime, ForeignKey, Integer
from sqlalchemy.sql import func
import uuid

from app.core.database import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # 레거시 chat_messages.session_id와 1:1 매핑되는 키
    legacy_session_id = Column(String(100), unique=True, index=True, nullable=False)

    owner_user_id = Column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    owner_guest_session_id = Column(String(36), ForeignKey("guest_sessions.id"), nullable=True, index=True)

    status = Column(String(20), nullable=False, default="active", index=True)
    mode = Column(String(20), nullable=True, index=True)  # actor / director

    title = Column(String(255), nullable=True)
    character_name = Column(String(100), nullable=True)
    preview_text = Column(Text, nullable=True)

    turn_count = Column(Integer, nullable=False, default=0)
    audio_count = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    last_message_at = Column(DateTime(timezone=True), nullable=True, index=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)
