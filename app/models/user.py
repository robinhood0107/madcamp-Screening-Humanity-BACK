"""
[역할]
인증 사용자 계정 모델 (현재 Google OAuth 기반).

[주의]
- provider 관련 필드는 향후 다중 OAuth 확장을 염두에 둔 흔적이 있으나, 현재는 google 중심이다.
- `id`는 문자열 UUID를 사용하며 다른 테이블 FK와 동일 규칙을 맞춘다.
"""

from sqlalchemy import Column, String, DateTime, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
import uuid
from app.core.database import Base

class User(Base):
    """애플리케이션 사용자 계정."""
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    email = Column(String(100), unique=True, index=True, nullable=False)
    username = Column(String(50), nullable=True) # Google profile name (nullable: 초기/실패 케이스 호환)
    picture = Column(String(255), nullable=True) # Google profile picture URL
    
    is_active = Column(Boolean, default=True)
    is_superuser = Column(Boolean, default=False)
    
    provider = Column(String(20), default="google") # 현재는 'google' 중심, 향후 provider 확장 가능성 고려
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationship to characters
    characters = relationship("Character", back_populates="user", cascade="all, delete-orphan")
    scenarios = relationship("Scenario", back_populates="user", cascade="all, delete-orphan")
    voices = relationship("Voice", back_populates="user")
