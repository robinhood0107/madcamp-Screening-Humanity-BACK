"""
[역할]
세션별 대화 요약(summary) 저장 테이블 모델.

[주의]
- `session_id`를 PK로 사용해 최신 요약 1개를 갱신하는 형태다.
- context_manager가 메모리/Redis/DB fallback을 섞어 쓰므로 호출 경로를 함께 봐야 한다.
"""

from sqlalchemy import Column, String, Text, DateTime
from sqlalchemy.sql import func
from app.core.database import Base

class ChatSummary(Base):
    """세션별 최신 요약 레코드."""
    __tablename__ = "chat_summaries"

    session_id = Column(String(100), primary_key=True, index=True)
    summary = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
