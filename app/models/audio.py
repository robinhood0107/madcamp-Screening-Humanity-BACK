"""
[역할]
TTS/업로드 등으로 생성된 오디오 파일 메타데이터를 저장한다.

[주의]
- 실제 바이너리는 파일시스템/스토리지에 있고, 이 테이블은 경로/URL/캐시 키 역할을 한다.
- `text_hash`는 로그인 사용자 TTS 캐시 재사용 경로에서 사용된다.
"""

from sqlalchemy import Column, String, DateTime, ForeignKey, Integer, Float
from sqlalchemy.sql import func
import uuid
from app.core.database import Base


class AudioFile(Base):
    """오디오 파일 메타데이터 모델 (파일 바이너리 자체가 아니라 조회/캐시용 메타 정보)."""
    __tablename__ = "audio_files"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    
    # 파일 정보
    file_path = Column(String(500), nullable=False)  # 저장 경로
    file_url = Column(String(500), nullable=False)  # 접근 URL
    file_size = Column(Integer, nullable=False)  # 파일 크기 (바이트)
    duration = Column(Float, nullable=True)  # 재생 시간 (초)
    format = Column(String(10), nullable=False)  # wav, ogg, aac, raw
    
    # TTS 정보
    voice_id = Column(String(50), nullable=True)  # 사용된 voice_id
    text_hash = Column(String(64), nullable=True, index=True)  # 텍스트 해시 (SHA256, 캐싱용)
    
    # 메타데이터
    created_at = Column(DateTime(timezone=True), server_default=func.now())
