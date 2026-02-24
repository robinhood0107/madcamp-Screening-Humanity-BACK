"""
[역할]
사용자가 선택한 상황/배경(시나리오) 설정을 저장하는 모델.

[주의]
- 채팅 메시지 전체를 직접 소유하지 않고, 대화 생성 전 설정 스냅샷 성격이 강하다.
"""

from sqlalchemy import Column, String, Text, DateTime, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
import uuid
from app.core.database import Base

class Scenario(Base):
    """시나리오 설정 스냅샷."""
    __tablename__ = "scenarios"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True) # dev-user 등 레거시/비인증 흐름 호환
    
    # Metadata
    user_name = Column(String(100), nullable=True) # 당시 설정한 사용자 이름
    character_name = Column(String(100), nullable=True) # 당시 설정한 캐릭터 이름
    situation = Column(Text, nullable=True) # 입력받은 상황 키워드
    
    # Generated Content
    summary = Column(Text, nullable=True) # 1줄 요약
    background = Column(Text, nullable=True) # 구체적 배경 설정 (AI System Prompt용)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationship
    user = relationship("User", back_populates="scenarios")
    # ChatMessages는 FK로 직접 묶지 않는다.
    # 현재 Scenario는 "대화 생성 전 설정" 역할을 유지한다(레거시 호환).
