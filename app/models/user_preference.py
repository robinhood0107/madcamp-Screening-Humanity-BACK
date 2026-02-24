"""
[역할]
사용자별 앱 설정(TTS 설정 등) 저장 모델.

[주의]
- 스키마를 자주 바꾸지 않기 위해 `settings` JSON 컬럼에 유연하게 저장한다.
- 키 구조 검증은 DB가 아니라 서비스/라우터 계층에서 담당한다.
"""

from sqlalchemy import Column, String, ForeignKey
from sqlalchemy import JSON
from app.core.database import Base


class UserPreference(Base):
    """사용자별 앱 설정 (TTS 옵션 등) 저장용 key-value JSON 컨테이너."""
    __tablename__ = "user_preferences"

    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    settings = Column(JSON, nullable=False, default=lambda: {})
