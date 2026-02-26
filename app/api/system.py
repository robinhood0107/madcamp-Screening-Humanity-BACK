"""
[역할]
운영 점검용 시스템/외부 의존성 상태 조회 라우터.

[주의]
- 이 파일의 헬스 체크는 "연결 가능 여부"를 우선 판정한다.
- 일부 4xx 응답도 online으로 간주하는 이유는 서비스가 살아있고 경로/메서드만 다를 수 있기 때문이다.
- 운영 환경에서는 상세 오류 메시지 노출 범위를 별도 정책으로 제한하는 2차 강화가 필요하다.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from app.core.database import get_db
from app.core.config import settings
from app.services.data_service_client import data_service_client
import httpx
import time
import asyncio

router = APIRouter()


def _health_message(status_code: int, is_ok: bool) -> str:
    """
    [역할]
    헬스 체크 응답의 `message`를 사용자/운영자가 이해하기 쉬운 문자열로 정규화한다.

    [왜 4xx도 연결됨으로 보나]
    이 엔드포인트는 "기능 성공"보다 "네트워크/서비스 도달 가능 여부"를 확인하는 용도이기 때문이다.
    """
    if not is_ok:  # 5xx
        return "연결됨 (5xx 서버 오류)"
    if status_code == 404:
        return "연결됨 (해당 경로 GET 미지원)"
    if status_code == 405:
        return "연결됨 (POST 전용 엔드포인트)"
    if status_code in (400, 422):
        return "연결됨 (파라미터 필요)"
    return f"Status: {status_code}"


async def check_http_service(url: str, timeout: float = 2.0) -> dict:
    """
    [역할]
    단일 HTTP 서비스에 GET 요청을 보내고 연결 가능 여부/지연시간을 표준 shape로 반환한다.

    [반환 계약]
    호출자는 `status`, `latency`, `message` 키를 기대하므로 1차 리팩토링에서 유지한다.
    """
    start_time = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            latency = (time.time() - start_time) * 1000
            is_ok = response.status_code < 500  # 4xx 정상, 5xx는 메시지만 구분
            # HTTP 응답이 오면 전부 online (연결 가능). 5xx는 메시지로 "서버 오류" 안내
            return {
                "status": "online",
                "latency": round(latency),
                "message": _health_message(response.status_code, is_ok),
            }
    except Exception as e:
        # broad except 유지 이유:
        # 운영 헬스 체크에서는 예외 타입보다 "offline + 원인 문자열"이 우선이며,
        # 개별 네트워크 예외를 모두 분기해도 응답 shape 이득이 작다.
        return {
            "status": "offline",
            "latency": 0,
            "message": str(e),
        }

@router.get("/health/detailed")
async def check_system_health(db: AsyncSession = Depends(get_db)):
    """
    [역할]
    DB + 외부 서비스(Ollama/TTS/Server A Files/Train) 상태를 단일 응답으로 반환한다.

    [호환성]
    프론트/운영 도구가 `success`, `timestamp`, `services` 구조를 사용하므로 유지한다.
    """
    # [왜 여기서 처리하나]
    # 이 라우터는 "DB + 외부 서비스 요약 대시보드"용이므로, 개별 실패를 즉시 raise 하지 않고 서비스별 상태로 축약한다.
    #
    # [주의]
    # 상세 오류를 문자열로 내려주는 현재 정책은 운영 디버깅 편의용이다. 2차 보안 강화 시 마스킹 범위를 재검토해야 한다.
    results = {}

    # 1. Database
    db_start = time.time()
    try:
        await db.execute(text("SELECT 1"))
        db_latency = (time.time() - db_start) * 1000
        results["database"] = {
            "name": "PostgreSQL DB",
            "status": "online",
            "latency": round(db_latency),
            "url": "Internal"
        }
    except Exception as e:
        # DB 헬스 체크도 개별 예외 타입을 나누기보다 offline 상태와 원인 문자열을 유지하는 쪽이 운영 화면에 유리하다.
        results["database"] = {
            "name": "PostgreSQL DB",
            "status": "offline",
            "latency": 0,
            "message": str(e),
            "url": "Internal"
        }

    # 1.5 C Data Service (gRPC rehearsal 포함)
    ds = data_service_client.health_check()
    ds_status = ds.get("status") or "unknown"
    results["data_service"] = {
        "name": "C Data Service (gRPC)",
        "status": "online" if ds_status == "grpc_ok" else ("degraded" if ds_status in {"scaffold_only", "grpc_unavailable"} else "offline"),
        "latency": 0,
        "message": ds.get("detail") or ds_status,
        "url": ds.get("grpc_addr") or settings.data_service_grpc_addr_value,
        "mode": "enabled" if ds.get("enabled") else "disabled",
        "grpc_status": ds_status,
    }

    # 2. External Services to check
    # (key, url, display_name, timeout초)
    # TTS/Train은 초기화 지연이 잦아 timeout을 상대적으로 길게 둔다.
    # TTS: /tts GET은 파라미터 없이 호출하면 422가 날 수 있지만 "연결 가능"로 간주한다.
    tts_health_url = f"{settings.TTS_BASE_URL.rstrip('/')}/{settings.TTS_API_PATH.lstrip('/')}"
    # Server A Files: 프록시 base + /api/health
    files_health_url = f"{settings.SERVER_A_FILES_API_URL.rstrip('/')}/api/health"
    # Server A Train: /api/health GET (200 정상). base는 gpuvoicetrain.duckdns.org
    train_health_url = f"{settings.SERVER_A_TRAINING_API_URL.rstrip('/')}/api/health"
    services = [
        ("ollama", f"{settings.OLLAMA_BASE_URL}", "Ollama (LLM)", 2.0),
        ("tts", tts_health_url, "GPT-SoVITS (TTS)", 5.0),
        ("server_a_files", files_health_url, "Server A (Files)", 2.0),
        ("server_a_train", train_health_url, "Server A (Train)", 5.0),
    ]

    # 서비스별 timeout을 유지하면서 병렬 체크한다. 한 서비스 지연이 전체 응답을 불필요하게 늦추지 않게 함.
    tasks = [check_http_service(url, t) for (_, url, _, t) in services]
    checks = await asyncio.gather(*tasks)

    for i, (key, url, name, _) in enumerate(services):
        res = checks[i]
        res["name"] = name
        # Server A (Files/Train)는 표시용으로 base만
        if key == "server_a_files":
            res["url"] = settings.SERVER_A_FILES_API_URL.rstrip("/")
        elif key == "server_a_train":
            res["url"] = settings.SERVER_A_TRAINING_API_URL.rstrip("/")
        else:
            res["url"] = url
        results[key] = res

    return {
        "success": True,
        "timestamp": time.time(),
        "services": results
    }
