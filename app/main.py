from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from app.core.config import settings
from app.core.database import engine, Base
from app.core.redis import init_redis_pool, close_redis_pool
from app.api import auth, users, generate
from contextlib import asynccontextmanager
import logging
import os

logger = logging.getLogger(__name__)


def _warn_deprecated_env_usage() -> None:
    """
    [역할]
    런타임에서 더 이상 쓰지 않는(또는 보안 정책상 금지된) env 사용을 감지해서 경고 로그를 남긴다.
    
    [왜 여기서 처리하나]
    startup 시점에 한 번만 경고해야 로그가 과도하게 반복되지 않는다.
    각 라우터에서 매번 경고하면 신호대잡음비가 너무 나빠진다.
    """
    if os.environ.get("NEXT_PUBLIC_GEMINI_API_KEY"):
        logger.warning(
            "NEXT_PUBLIC_GEMINI_API_KEY detected but ignored by policy. "
            "Use GEMINI_API_KEY in madcamp-Screening-Humanity-BACK/.env."
        )


def _log_security_flag_status() -> None:
    """
    [역할]
    보안 민감 플래그의 현재 값을 startup 로그에 남긴다.

    [주의]
    1차 리팩토링은 기본 동작을 유지하는 단계라서 insecure/dev fallback 옵션이 켜져 있어도 서버를 막지 않는다.
    대신 운영자가 바로 볼 수 있게 경고를 남긴다.
    """
    if settings.DEV_AUTH_FALLBACK_ENABLED:
        logger.warning("DEV_AUTH_FALLBACK_ENABLED=true (운영에서는 false 권장)")
    if settings.GOOGLE_SSO_ALLOW_INSECURE_HTTP:
        logger.warning("GOOGLE_SSO_ALLOW_INSECURE_HTTP=true (운영 HTTPS에서는 false 권장)")
    if not settings.AUTH_COOKIE_SECURE:
        logger.warning("AUTH_COOKIE_SECURE=false (운영 HTTPS에서는 true 권장)")


def _run_startup_validation_checks() -> None:
    """
    [역할]
    startup 시 운영 안전성과 관련된 설정 상태를 warn-only로 점검한다.

    [주의]
    - `STRICT_STARTUP_VALIDATION`는 1차 리팩토링에서 차단 모드가 아니라 경고 강화 모드다.
    - 기본값(false)에서는 조용히 return 하여 기존 동작을 유지한다.
    """
    if not settings.STRICT_STARTUP_VALIDATION:
        return

    logger.warning("STRICT_STARTUP_VALIDATION=true (warn-only): startup 설정 점검 강화 모드")
    is_prod = settings.app_env_normalized == "production"

    if settings.SECRET_KEY == "YOUR_SECRET_KEY_HERE_CHANGE_IN_PROD":
        logger.warning("SECRET_KEY가 기본 예시값입니다. 운영 환경에서는 반드시 변경하세요.")
    if settings.DEV_AUTH_FALLBACK_ENABLED:
        if is_prod:
            raise RuntimeError(
                "STRICT_STARTUP_VALIDATION: APP_ENV=production 에서 DEV_AUTH_FALLBACK_ENABLED=true 는 허용되지 않습니다."
            )
        logger.warning("STRICT_STARTUP_VALIDATION: DEV_AUTH_FALLBACK_ENABLED=true 상태")
    if settings.GOOGLE_SSO_ALLOW_INSECURE_HTTP:
        logger.warning("STRICT_STARTUP_VALIDATION: GOOGLE_SSO_ALLOW_INSECURE_HTTP=true 상태")
    if not settings.AUTH_COOKIE_SECURE:
        logger.warning("STRICT_STARTUP_VALIDATION: AUTH_COOKIE_SECURE=false 상태")
    if not settings.GEMINI_API_KEY:
        logger.warning("STRICT_STARTUP_VALIDATION: GEMINI_API_KEY 미설정 (Gemini 경로 비활성 가능)")
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        logger.warning("STRICT_STARTUP_VALIDATION: Google OAuth 클라이언트 설정 누락 가능")


def _ensure_runtime_dirs() -> None:
    """
    [역할]
    런타임에 필요한 로컬 디렉터리를 미리 만든다.

    [부작용]
    - USER_ASSETS_DIR 디렉터리 생성
    """
    os.makedirs(settings.USER_ASSETS_DIR, exist_ok=True)


async def _apply_startup_schema_patches(conn) -> None:
    """
    [역할]
    레거시 DB와의 호환을 위해 startup 시 ad-hoc DDL 패치를 적용한다.

    [왜 함수로 분리했나]
    기존에는 lifespan 안에 DDL/테이블 생성/예외처리가 모두 섞여 있어서 읽기 어려웠다.
    startup 동작을 바꾸지 않으면서도 위험 구간(DDL)을 한곳에 모아두면 추후 마이그레이션 도구로 교체하기 쉽다.

    [주의]
    - 운영에서 재현성 이슈가 있을 수 있으므로 2차 리팩토링에서는 Alembic 전환 후보이다.
    - 1차에서는 STARTUP_SCHEMA_PATCH_ENABLED 플래그로만 제어하고 기본값은 기존 동작 유지(true).
    """
    if not settings.STARTUP_SCHEMA_PATCH_ENABLED:
        logger.warning("STARTUP_SCHEMA_PATCH_ENABLED=false: startup schema patch 생략")
        return

    # 기존 DB: voices에 user_id/train_input_dir/training_model_name 컬럼 추가 (이미 있으면 무시)
    for col, typ in [
        ("user_id", "VARCHAR(36)"),
        ("train_input_dir", "VARCHAR(500)"),
        ("training_model_name", "VARCHAR(200)"),
    ]:
        try:
            await conn.execute(text(f"ALTER TABLE voices ADD COLUMN {col} {typ}"))
        except Exception as e:
            lower = str(e).lower()
            if "duplicate column" not in lower and "already exists" not in lower:
                raise

    # user_ref_sounds 테이블 삭제 (drop_only, 없으면 무시)
    try:
        await conn.execute(text("DROP TABLE IF EXISTS user_ref_sounds"))
    except Exception as e:
        logger.info("user_ref_sounds DROP TABLE 생략 또는 실패: %s", e)

    # characters.sample_dialogue 컬럼 제거 (SQLite 3.35+)
    try:
        await conn.execute(text("ALTER TABLE characters DROP COLUMN sample_dialogue"))
    except Exception as e:
        logger.info("characters.sample_dialogue DROP COLUMN 생략 또는 실패: %s", e)


def _log_cors_configuration() -> None:
    origins = settings.backend_cors_allow_origins
    logger.info("CORS allow_origins=%s, allow_credentials=%s", origins, True)


# Lifecycle for DB creation
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_redis_pool()
    _warn_deprecated_env_usage()
    _log_security_flag_status()
    _run_startup_validation_checks()
    _ensure_runtime_dirs()
    
    async with engine.begin() as conn:
        # Create tables
        from app.models import (
            user,
            generation,
            character,
            audio,
            summary,
            voice,
            scenario,
            chat_message,
            user_preference,
            guest_session,
            auth_event,
        )
        await conn.run_sync(Base.metadata.create_all)
        await _apply_startup_schema_patches(conn)
    yield
    # Shutdown
    await close_redis_pool()
    await engine.dispose()

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan
)

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.backend_cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=3600,
)

_log_cors_configuration()

# Static Files Mount
# /assets 경로로 접근 시 USER_ASSETS_DIR의 파일을 서빙
app.mount("/assets", StaticFiles(directory=settings.USER_ASSETS_DIR), name="assets")

# Routers
app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/auth", tags=["auth"])
app.include_router(users.router, prefix=f"{settings.API_V1_STR}/users", tags=["users"])
app.include_router(generate.router, prefix=f"{settings.API_V1_STR}/generate", tags=["generate"])

from app.api import chat, characters, story, evaluation
app.include_router(story.router, prefix=f"{settings.API_V1_STR}/story", tags=["story"])
app.include_router(evaluation.router, prefix=f"{settings.API_V1_STR}/evaluation", tags=["evaluation"])
app.include_router(chat.router, prefix=f"{settings.API_V1_STR}", tags=["chat"])

from app.api import tts
app.include_router(tts.router, prefix=f"{settings.API_V1_STR}", tags=["tts"])

from app.api import voices
app.include_router(voices.router, prefix=f"{settings.API_V1_STR}", tags=["voices"])

from app.api import ai
app.include_router(ai.router, prefix=f"{settings.API_V1_STR}/ai", tags=["ai"])

from app.api import characters
app.include_router(characters.router, prefix=f"{settings.API_V1_STR}", tags=["characters"])

from app.api import system
app.include_router(system.router, prefix=f"{settings.API_V1_STR}/system", tags=["system"])

from app.api import model_make
app.include_router(model_make.router, prefix=f"{settings.API_V1_STR}/model-make", tags=["model-make"])

# 레거시 경로 호환성 추가
app.include_router(ai.router, prefix=f"{settings.API_V1_STR}", tags=["legacy"])

@app.get("/")
async def root():
    return {"success": True, "message": "Avatar Forge Backend Running"}

@app.get(f"{settings.API_V1_STR}/system/health")
@app.get("/api/health") # Legacy/Direct health
async def health():
    return {"success": True, "data": {"status": "healthy"}}

@app.get(f"{settings.API_V1_STR}/system/status")
async def system_status():
    return {
        "success": True,
        "data": {
            "status": "online",
            "version": "1.0.0",
            "gpu_server": "connected"
        }
    }

@app.get("/api/test")
async def test_connection():
    """연결 테스트 엔드포인트"""
    return {
        "status": "connected",
        "message": "백엔드 서버에 정상적으로 연결되었습니다.",
        "cors_origins": [str(origin) for origin in settings.BACKEND_CORS_ORIGINS] if settings.BACKEND_CORS_ORIGINS else ["*"]
    }
