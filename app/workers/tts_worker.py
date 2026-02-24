import asyncio
import json
import httpx
import redis.asyncio as redis
import os
import sys
import logging

# 프로젝트 루트를 sys.path에 추가하여 app 모듈을 찾을 수 있게 함
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.core.config import settings

logger = logging.getLogger(__name__)

# Worker 상태 (현재 로드된 모델)
_current_gpt_path: str = None
_current_sovits_path: str = None

QUEUE_KEYS = [
    "tts:queue:realtime",
    "tts:queue:delayed",
    "tts:queue:on_click",
]
STREAM_TTL_SECONDS = 60


def _decode_job_payload(job_data):
    """
    [역할]
    Redis BRPOP 결과(job_data)를 dict로 디코드한다.
    """
    if isinstance(job_data, bytes):
        job_data = job_data.decode("utf-8")
    return json.loads(job_data)


async def _xadd_error(redis_client, stream_key: str, message: str):
    """
    [역할]
    스트림으로 에러 이벤트를 밀어 넣는 공통 helper.
    """
    # [보안/운영]
    # 외부 오류 응답 원문 전체를 그대로 내보내면 민감정보/과도한 payload가 섞일 수 있어 길이를 제한한다.
    safe_message = (message or "")[:500]
    await redis_client.xadd(stream_key, {"type": "error", "message": safe_message})


async def _set_weight_if_needed(
    client: httpx.AsyncClient,
    *,
    tts_base: str,
    endpoint: str,
    label: str,
    weights_path: str | None,
    current_path: str | None,
) -> str | None:
    """
    [역할]
    현재 로드된 가중치와 다를 때만 Server A에 가중치 전환 요청을 보낸다.
    """
    if not weights_path or weights_path == current_path:
        return current_path

    try:
        logger.info("Setting %s weights: %s", label, weights_path)
        resp = await client.get(f"{tts_base}/{endpoint}", params={"weights_path": weights_path})
        if resp.status_code == 200:
            return weights_path
        logger.warning("Failed to set %s weights: status=%s body=%s", label, resp.status_code, resp.text)
        return current_path
    except Exception as e:
        logger.warning("Error setting %s weights: %s", label, e)
        return current_path


async def _relay_tts_stream(
    client: httpx.AsyncClient,
    redis_client,
    *,
    stream_key: str,
    tts_url: str,
    request_body: dict,
) -> int:
    """
    [역할]
    Server A TTS 스트리밍 응답을 Redis Stream으로 relay한다.

    [왜 여기서 처리하나]
    - `run_worker` 본문에서 가장 길고 사고 포인트가 많은 부분이라 분리해 두면
      에러 처리/Relay 포맷을 한 곳에서 볼 수 있다.
    """
    try:
        async with client.stream("POST", tts_url, json=request_body) as response:
            if response.status_code != 200:
                error_text = await response.read()
                err_body = error_text.decode("utf-8", errors="ignore")
                err_msg = f"Server A error: {response.status_code} {err_body[:400]}"
                logger.warning(err_msg)
                await _xadd_error(redis_client, stream_key, err_msg)
                return 0

            chunk_count = 0
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                chunk_count += 1
                await redis_client.xadd(stream_key, {"type": "chunk", "data": chunk})

            await redis_client.xadd(stream_key, {"type": "eof"})
            return chunk_count
    except Exception as e:
        logger.warning("TTS Request failed: %s", e)
        await _xadd_error(redis_client, stream_key, str(e))
        return 0


async def _process_single_job(
    redis_client,
    client: httpx.AsyncClient,
    *,
    queue_key,
    job: dict,
):
    """
    [역할]
    큐에서 꺼낸 단일 TTS job을 처리한다. (가중치 전환 + 스트림 relay + TTL)

    [주의]
    - Worker 전역 상태(`_current_gpt_path`, `_current_sovits_path`)를 갱신한다.
    - request_body는 in-place로 정리(pop)하므로 재사용하지 않는 전제가 필요하다.
    """
    global _current_gpt_path, _current_sovits_path

    job_id = job["job_id"]
    gpt_path = job.get("gpt_weights_path")
    sovits_path = job.get("sovits_weights_path")
    request_body = job["request_body"]
    stream_key = f"tts:stream:{job_id}"

    logger.info("Processing job %s from %s", job_id, queue_key)

    tts_base = settings.TTS_BASE_URL.rstrip("/")
    _current_gpt_path = await _set_weight_if_needed(
        client,
        tts_base=tts_base,
        endpoint="set_gpt_weights",
        label="GPT",
        weights_path=gpt_path,
        current_path=_current_gpt_path,
    )
    _current_sovits_path = await _set_weight_if_needed(
        client,
        tts_base=tts_base,
        endpoint="set_sovits_weights",
        label="SoVITS",
        weights_path=sovits_path,
        current_path=_current_sovits_path,
    )

    # Worker relay 경로에서는 가중치 정보는 모델 스위칭에만 쓰고, TTS body에서는 제거한다.
    request_body.pop("gpt_weights", None)
    request_body.pop("sovits_weights", None)

    tts_url = f"{tts_base}/{settings.TTS_API_PATH.lstrip('/')}"
    chunk_count = await _relay_tts_stream(
        client,
        redis_client,
        stream_key=stream_key,
        tts_url=tts_url,
        request_body=request_body,
    )
    if chunk_count:
        logger.info("Job %s completed. %s chunks relayed.", job_id, chunk_count)

    await redis_client.expire(stream_key, STREAM_TTL_SECONDS)

async def run_worker():
    """
    TTS Worker 메인 루프
    Redis 큐(BRPOP) -> set_weights -> POST /tts (stream) -> Redis Stream(XADD)
    """
    global _current_gpt_path, _current_sovits_path
    
    # [주의]
    # Worker는 장시간 떠 있는 프로세스라서 print보다 구조화된 logger가 운영 추적에 유리하다.
    # 1차에서는 메시지 의미만 유지하고 출력 수단만 logger로 바꾼다.
    logger.info("Connecting to Redis: %s", settings.REDIS_URL)
    redis_client = redis.from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=False)
    client = httpx.AsyncClient(timeout=None) # 타임아웃 없음 (스트리밍 위해)
    
    logger.info("TTS Worker started. Waiting for jobs...")
    
    try:
        while True:
            # 1. Job 수신 (Priority: realtime > delayed > on_click)
            keys = QUEUE_KEYS
            
            # BRPOP: (key, value) 튜플 반환. 타임아웃 0(무한 대기)
            try:
                # redis-py의 brpop은 타임아웃 0이면 무한대기
                # 연결이 끊어지면 예외 발생 -> 아래 catch
                result = await redis_client.brpop(keys, timeout=0)
                if not result:
                    continue
                    
                queue_key, job_data = result
                # job_data 디코드/JSON 파싱은 helper에서 일괄 처리한다.
                job = _decode_job_payload(job_data)
                await _process_single_job(
                    redis_client,
                    client,
                    queue_key=queue_key,
                    job=job,
                )

            except redis.ConnectionError:
                logger.warning("Redis connection lost. Retrying in 1s...")
                await asyncio.sleep(1)
            except Exception as e:
                logger.exception("Worker loop error: %s", e)
                await asyncio.sleep(1)
                
    finally:
        await client.aclose()
        await redis_client.close()

if __name__ == "__main__":
    # 윈도우/Python 3.8+ asyncio 정책
    import sys
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    asyncio.run(run_worker())
