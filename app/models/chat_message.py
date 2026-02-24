"""
[역할]
채팅 메시지 저장 테이블 모델.

[주의]
- Scenario와는 FK가 아니라 `session_id` 기반으로 느슨하게 연결되는 경로가 존재한다.
- streaming 라우트에서는 저장 정책이 일부 다를 수 있으므로 호출자 문맥을 함께 확인해야 한다.
"""

from sqlalchemy import Column, String, Text, DateTime, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
import uuid
from app.core.database import Base

class ChatMessage(Base):
    """채팅 세션별 메시지 로그."""
    __tablename__ = "chat_messages"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(100), index=True, nullable=False) # 채팅 세션 식별자 (UUID 문자열)
    
    role = Column(String(20), nullable=False) # user, assistant, system
    content = Column(Text, nullable=False)
    
    # Optional metadata (프론트 표시/디버깅용 보조 정보)
    character_name = Column(String(100), nullable=True) # 화자 이름 (AI인 경우)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
