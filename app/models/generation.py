"""
[역할]
비동기 생성 작업(3D/style/tts 등)의 상태 추적용 테이블 모델.

[주의]
- `input_payload`는 현재 JSON 문자열/경로 문자열 등 혼합 저장이 가능해 타입 정규화가 완전하지 않다.
- 1차 리팩토링에서는 스키마 변경 없이 라우터/서비스에서 해석 규칙을 유지한다.
"""

from sqlalchemy import Column, String, DateTime, ForeignKey, Text, Float, Integer, Boolean
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
import uuid
from app.core.database import Base

class GenerationJob(Base):
    """생성 작업 상태/결과 메타데이터."""
    __tablename__ = "generation_jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True) # Nullable for anonymous if needed
    
    job_type = Column(String(20), nullable=False) # '3d', 'style', 'tts'
    status = Column(String(20), default="pending") # 'pending', 'processing', 'completed', 'failed'
    
    input_payload = Column(Text, nullable=True) # JSON string of inputs or path to input file
    
    result_url = Column(String(255), nullable=True)
    error_message = Column(Text, nullable=True)
    
    progress = Column(Integer, default=0)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
