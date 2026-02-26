"""
[역할]
캐릭터 카탈로그 변경 이력을 기록한다. (관리자 운영 추적/롤백 보조)

[주의]
- 1차에서는 create/update/delete/seed_import 위주로 기록한다.
- before/after JSON은 SQLite 브리지에서도 동작하도록 SQLAlchemy JSON 타입 사용.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.sql import func
from sqlalchemy.types import JSON

from app.core.database import Base


class CharacterCatalogRevision(Base):
    __tablename__ = "character_catalog_revisions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    character_id = Column(String(36), ForeignKey("character_catalog.id"), nullable=False, index=True)
    changed_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    action = Column(String(40), nullable=False, index=True)  # create/update/delete/seed_import/restore/...
    before_data = Column(JSON, nullable=True)
    after_data = Column(JSON, nullable=True)
    change_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

