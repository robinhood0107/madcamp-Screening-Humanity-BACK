#!/usr/bin/env python3
"""
FRONT/public/characters + public/images/characters 를 DB character_catalog로 seed import 하는 보조 스크립트.

[용도]
- 관리자 API를 쓰기 전/독립적으로 dry-run 및 실제 import 수행
- SQLite 브리지/향후 PostgreSQL 전환 환경 모두에서 1차 seed 검증
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

# BACK/scripts -> BACK
BACK_ROOT = Path(__file__).resolve().parents[1]
if str(BACK_ROOT) not in sys.path:
    sys.path.insert(0, str(BACK_ROOT))

from app.core.database import AsyncSessionLocal, engine, Base  # noqa: E402
from app.models import user, character_catalog, character_catalog_revision, media_asset  # noqa: F401,E402
from app.services import character_catalog_service  # noqa: E402


async def _run(*, dry_run: bool, limit: int | None, upsert: bool) -> int:
    # SQLite/로컬 브리지에서도 테이블이 없을 수 있어 create_all 보수적으로 호출
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as db:
        result = await character_catalog_service.seed_import_from_front_public(
            db=db,
            changed_by_user_id=None,
            dry_run=dry_run,
            limit=limit,
            upsert=upsert,
        )
        if dry_run:
            await db.rollback()
        else:
            await db.commit()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed import character catalog from FRONT/public/characters")
    parser.add_argument("--dry-run", action="store_true", help="DB commit 없이 결과만 출력")
    parser.add_argument("--limit", type=int, default=None, help="처리할 최대 JSON 파일 수")
    parser.add_argument("--no-upsert", action="store_true", help="기존 slug가 있으면 skip")
    args = parser.parse_args()
    return asyncio.run(_run(dry_run=args.dry_run, limit=args.limit, upsert=not args.no_upsert))


if __name__ == "__main__":
    raise SystemExit(main())
