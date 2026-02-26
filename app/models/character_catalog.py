"""
[역할]
DB 원본화 대상 캐릭터 카탈로그(preset/admin/user origin 공통)를 저장한다.

[주의]
- 1차 브리지 구현에서는 `image_asset_id`를 아직 쓰지 않고 `image_url_external` 중심으로 운영한다.
- PostgreSQL 전환 시 JSONB가 되지만, SQLAlchemy JSON 타입으로 SQLite 브리지도 동작 가능하게 유지한다.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Text
from sqlalchemy.sql import func
from sqlalchemy.types import JSON

from app.core.database import Base


class CharacterCatalog(Base):
    __tablename__ = "character_catalog"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    slug = Column(String(120), unique=True, index=True, nullable=False)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    persona = Column(Text, nullable=True)
    category = Column(String(100), nullable=True, index=True)
    tags = Column(JSON, nullable=False, default=list)

    # PostgreSQL canonical schema와의 정합성을 위해 컬럼명 유지 (1차는 nullable + 미사용 가능)
    image_asset_id = Column(String(36), nullable=True, index=True)
    image_url_external = Column(String(500), nullable=True)
    voice_id = Column(String(64), nullable=True)

    origin_type = Column(String(40), nullable=False, default="admin_created", index=True)
    is_public = Column(Boolean, nullable=False, default=True, index=True)
    is_preset = Column(Boolean, nullable=False, default=False, index=True)

    created_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    updated_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    seed_source_path = Column(String(500), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

