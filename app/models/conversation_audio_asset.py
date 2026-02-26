"""
[역할]
대화 메시지와 TTS 생성 오디오(파일 시스템/DB 메타)를 연결하는 history 보조 테이블.

[주의]
- 현재 1차 구현에서는 `audio_files`(로그인 사용자 캐시)와 익명/guest 파일이 공존하므로
  `audio_file_id`는 optional이고 `file_url/file_path`를 함께 보관한다.
- 파일 삭제는 DB 트랜잭션과 완전 원자적이지 않으므로 호출자에서 보상/로그 정책을 유지해야 한다.
"""

from sqlalchemy import Column, String, DateTime, ForeignKey, Float, Integer
from sqlalchemy.sql import func
import uuid

from app.core.database import Base


class ConversationAudioAsset(Base):
    __tablename__ = "conversation_audio_assets"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    conversation_id = Column(String(36), ForeignKey("conversations.id"), nullable=False, index=True)
    message_id = Column(String(36), nullable=True, index=True)

    owner_user_id = Column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    owner_guest_session_id = Column(String(36), ForeignKey("guest_sessions.id"), nullable=True, index=True)

    audio_file_id = Column(String(36), nullable=True, index=True)  # app.models.audio.AudioFile.id (로그인 캐시 경로)
    file_id = Column(String(64), nullable=True, index=True)  # anonymous/guest 저장 응답 file_id 포함
    file_url = Column(String(500), nullable=False)
    file_path = Column(String(500), nullable=True)
    voice_id = Column(String(50), nullable=True)
    duration_sec = Column(Float, nullable=True)
    mime_type = Column(String(100), nullable=True)
    size_bytes = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)
