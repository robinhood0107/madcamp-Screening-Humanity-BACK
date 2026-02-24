import redis.asyncio as redis
from typing import Optional
import logging
from app.core.config import settings

# 전역 Redis 클라이언트 인스턴스
redis_client: Optional[redis.Redis] = None
logger = logging.getLogger(__name__)

async def init_redis_pool():
    """애플리케이션 시작 시 Redis 연결 풀 초기화"""
    global redis_client
    redis_client = redis.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=False,  # 바이너리 데이터(오디오 청크) 처리를 위해 False
        max_connections=10,
    )
    # 연결 테스트
    try:
        await redis_client.ping()
        logger.info("Redis connected: %s", settings.REDIS_URL)
    except Exception as e:
        logger.warning("Redis connection failed: %s", e)

async def close_redis_pool():
    """애플리케이션 종료 시 Redis 연결 해제"""
    global redis_client
    if redis_client:
        await redis_client.close()
        logger.info("Redis connection closed")

def get_redis_client() -> redis.Redis:
    """Redis 클라이언트 반환"""
    if redis_client is None:
        # 워커 프로세스 등에서 lifespan 이벤트 없이 사용할 경우를 대비해 lazy initialization
        # (하지만 lifespan을 쓰는 게 좋음)
        raise RuntimeError("Redis client is not initialized. Ensure 'init_redis_pool' is called.")
    return redis_client
