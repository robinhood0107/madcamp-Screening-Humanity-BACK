"""
[역할]
하이브리드 저장(Blob/Volume/S3 호환)을 위한 공통 미디어 자산 메타/데이터 테이블 모델.

[주의]
- 현재 Python 브리지 단계에서는 character_catalog 이미지 업로드/seed import 중심으로 먼저 사용한다.
- PostgreSQL canonical schema와 정합성을 맞추되 SQLite 브리지 환경에서도 동작하도록 타입을 보수적으로 둔다.
"""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, LargeBinary, String
from sqlalchemy.sql import func

from app.core.database import Base


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    owner_user_id = Column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    owner_guest_session_id = Column(String(36), ForeignKey("guest_sessions.id"), nullable=True, index=True)

    asset_kind = Column(String(50), nullable=False, index=True)  # chat_audio/tts_audio/character_image/...
    storage_backend = Column(String(32), nullable=False, default="volume", index=True)  # pg_blob/volume/s3_compatible

    mime_type = Column(String(100), nullable=True)
    size_bytes = Column(BigInteger, nullable=False, default=0)
    sha256 = Column(String(64), nullable=True, index=True)

    filename_original = Column(String(500), nullable=True)
    filename_stored = Column(String(500), nullable=True)

    blob_data = Column(LargeBinary, nullable=True)
    volume_path = Column(String(1000), nullable=True)
    object_key = Column(String(1000), nullable=True)

    status = Column(String(20), nullable=False, default="active", index=True)  # active/deleted_soft/orphaned
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)

