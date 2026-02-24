"""
[역할]
캐릭터(페르소나) 정보를 저장하는 핵심 테이블 모델.

[주의]
- preset 캐릭터는 파일(JSON) 기반도 존재하므로, 라우터/서비스는 DB와 preset 파일을 함께 다룬다.
- `tags`는 현재 JSON 문자열(Text)로 저장되며 1차 리팩토링에서는 스키마를 바꾸지 않는다.
"""

from sqlalchemy import Column, String, DateTime, Boolean, ForeignKey, Text
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
import uuid
from app.core.database import Base


class Character(Base):
    """
    캐릭터 모델 (DB 저장 대상).
    사용자 생성 캐릭터 + 일부 시스템 캐릭터 메타를 담는다.
    """
    __tablename__ = "characters"

    id = Column(String(36), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), nullable=False)
    description = Column(String(255), nullable=True)
    
    # 페르소나 설정
    persona = Column(Text, nullable=True)
    
    # 음성 설정 (Voice 모델과 연동)
    voice_id = Column(String(36), ForeignKey("voices.id"), nullable=True)
    
    # 카테고리 및 태그
    category = Column(String(50), nullable=True)
    tags = Column(Text, nullable=True)  # JSON 문자열로 저장
    
    # 이미지
    image_url = Column(String(255), nullable=True)
    
    # 3D 모델 관련
    model_url = Column(String(255), nullable=True)
    thumbnail_url = Column(String(255), nullable=True)

    is_preset = Column(Boolean, default=False)  # True여도 preset JSON과 완전 동기 보장은 아님(레거시 호환)
    
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True)  # 시스템/관리자 공용 캐릭터는 nullable 가능
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())

    # Relationship
    user = relationship("User", back_populates="characters")
    voice = relationship("Voice", back_populates="characters")
