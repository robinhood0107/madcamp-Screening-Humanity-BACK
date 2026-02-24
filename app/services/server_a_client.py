"""
Server A (GPT-SoVITS automation + TTS) 공통 클라이언트.

[의도]
- `voices.py`, `model_make.py`, `tts.py`에 흩어진 httpx 호출 패턴을 한 곳으로 모은다.
- 외부 계약(엔드포인트 path/응답 shape)은 유지하고, URL 조합/timeout/연결 실패 처리만 공통화한다.

[주의]
- 1차는 "보수적 공통화" 단계라서 모든 상태코드/메시지를 완전히 통일하지 않는다.
- 각 라우터가 커스텀 에러 메시지를 유지해야 하는 경우 response를 받아서 직접 처리하게 둔다.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
import logging

import httpx
from fastapi import HTTPException, status

from app.core.config import settings

logger = logging.getLogger(__name__)


def _join_url(base: str, path: str) -> str:
    """
    [역할]
    base/path를 중복 슬래시 없이 결합한다.

    [왜 여기서 처리하나]
    - 라우터/서비스마다 `rstrip/lstrip` URL 조합이 반복되던 패턴을 한 곳에 고정한다.

    [주의]
    - query string 결합용이 아니라 path 결합용 helper다.
    """
    return f"{(base or '').rstrip('/')}/{(path or '').lstrip('/')}"


def _service_base(service: str) -> str:
    """
    [역할]
    서비스 키(files/training/tts)에 맞는 Server A base URL을 반환.

    [왜 여기서 처리하나]
    - `voices.py`/`model_make.py`/`tts.py`가 서로 다른 base URL 설정 키를 직접 알지 않게 만든다.

    [주의]
    - 지원 서비스 키 추가 시 라우터보다 먼저 여기와 문서를 같이 갱신해야 한다.
    """
    key = (service or "").strip().lower()
    if key == "files":
        return settings.server_a_files_api_base_url
    if key == "training":
        return settings.server_a_training_api_base_url
    if key == "tts":
        return settings.TTS_BASE_URL.rstrip("/")
    raise ValueError(f"Unsupported Server A service: {service}")


def _service_label(service: str) -> str:
    """
    [역할]
    로그/에러 메시지용 사람 친화적 서비스 라벨을 반환한다.

    [왜 여기서 처리하나]
    - 연결 실패 로그/HTTPException detail 메시지의 표현을 한 곳에서 맞추기 위해서다.
    """
    key = (service or "").strip().lower()
    if key == "files":
        return "Server A 파일 API"
    if key == "training":
        return "Server A 학습 API"
    if key == "tts":
        return "Server A TTS API"
    return "Server A API"


async def _request(
    *,
    service: str,
    method: str,
    path: str,
    timeout: float,
    params: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    files: Any = None,
    verify: Optional[bool] = None,
    request_error_detail: Optional[str] = None,
) -> httpx.Response:
    """
    [역할]
    Server A 요청 공통 래퍼. 연결 실패(RequestError)는 HTTPException(503)로 변환한다.

    [왜 여기서 처리하나]
    - 라우터마다 반복되는 base URL 조합 + AsyncClient 생성 + RequestError 처리 중복 제거.
    - 상태코드(200/404 등) 해석은 각 라우터가 유지해 호환성을 보장한다.

    [부작용]
    - 외부 HTTP 요청
    - 연결 실패 시 경고 로그 기록

    [실패/예외]
    - `httpx.RequestError`만 여기서 `503 HTTPException`으로 변환한다.
    - non-200 상태코드 처리는 라우터 또는 `ensure_success_response()`가 담당한다.

    [주의]
    - `service='tts'`는 `settings.TTS_SSL_VERIFY`를 기본 적용한다. 운영 장애 시 SSL verify 설정부터 먼저 확인한다.
    """
    base_url = _service_base(service)
    url = _join_url(base_url, path)
    method = (method or "GET").upper()

    # TTS는 SSL verify 설정이 따로 있어서 settings값을 기본으로 반영.
    if verify is None and service == "tts":
        verify = settings.TTS_SSL_VERIFY

    client_kwargs: Dict[str, Any] = {"timeout": timeout}
    if verify is not None:
        client_kwargs["verify"] = verify

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.request(
                method,
                url,
                params=params,
                data=data,
                json=json_body,
                files=files,
            )
            return response
    except httpx.RequestError as e:
        logger.warning("%s request failed: method=%s url=%s err=%s", _service_label(service), method, url, e)
        detail = request_error_detail or f"{_service_label(service)} 연결 실패: {str(e)}"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail,
        ) from e


def ensure_success_response(
    response: httpx.Response,
    *,
    error_prefix: str,
    allow_404_detail: Optional[str] = None,
) -> httpx.Response:
    """
    [역할]
    non-200 응답을 FastAPI HTTPException으로 변환하는 공통 헬퍼.

    [왜 여기서 처리하나]
    - 라우터별 `if response.status_code != 200` 반복을 줄이되, 사용자-facing 메시지 제어권은 라우터에 남긴다.

    [실패/예외]
    - 200이 아니면 `HTTPException`을 발생시킨다.

    [주의]
    - 404는 라우터별 메시지 유지가 필요한 경우 `allow_404_detail`로 전달.
    - 응답 본문(`response.text`)을 그대로 detail에 붙이므로, 라우터에서 더 안전한 메시지가 필요하면 이 helper를 우회/보완한다.
    """
    if response.status_code == 200:
        return response

    if response.status_code == 404 and allow_404_detail:
        raise HTTPException(status_code=404, detail=allow_404_detail)

    raise HTTPException(
        status_code=response.status_code,
        detail=f"{error_prefix}: {response.text}",
    )


# ========= files API wrappers =========

async def get_files_index(*, timeout: float = 10.0) -> httpx.Response:
    return await _request(
        service="files",
        method="GET",
        path="/api/files/all",
        timeout=timeout,
        request_error_detail=f"Server A 파일 API({settings.SERVER_A_FILES_API_URL})에 연결할 수 없습니다",
    )


async def get_files_models(*, timeout: float = 10.0) -> httpx.Response:
    return await _request(service="files", method="GET", path="/api/files/models", timeout=timeout)


async def get_files_train_voices(*, timeout: float = 10.0) -> httpx.Response:
    return await _request(service="files", method="GET", path="/api/files/train-voices", timeout=timeout)


async def get_files_logs(*, timeout: float = 10.0) -> httpx.Response:
    return await _request(service="files", method="GET", path="/api/files/logs", timeout=timeout)


async def upload_train_voice_file(*, file_tuple: Any, sub_path: str, timeout: float = 300.0) -> httpx.Response:
    return await _request(
        service="files",
        method="POST",
        path="/api/files/upload",
        timeout=timeout,
        data={"category": "train_voice", "sub_path": sub_path},
        files={"file": file_tuple},
    )


async def upload_training_item(*, files: Any, sub_path: str, model_version: str = "v2", timeout: float = 120.0) -> httpx.Response:
    return await _request(
        service="files",
        method="POST",
        path="/api/files/upload",
        timeout=timeout,
        data={"category": "train_voice", "sub_path": sub_path, "model_version": model_version},
        files=files,
    )


async def delete_file(*, path: str, timeout: float = 10.0) -> httpx.Response:
    return await _request(
        service="files",
        method="DELETE",
        path="/api/files",
        timeout=timeout,
        params={"path": path},
    )


async def mkdir(*, path: str, timeout: float = 10.0) -> httpx.Response:
    return await _request(
        service="files",
        method="POST",
        path="/api/files/mkdir",
        timeout=timeout,
        data={"path": path},
    )


async def trim_audio(*, source_path: str, max_duration_sec: float, output_suffix: str = "_trimmed", timeout: float = 30.0) -> httpx.Response:
    return await _request(
        service="files",
        method="POST",
        path="/api/files/trim-audio",
        timeout=timeout,
        data={
            "source_path": source_path,
            "max_duration_sec": max_duration_sec,
            "output_suffix": output_suffix,
        },
    )


async def validate_ref_audio(*, path: str, timeout: float = 10.0) -> httpx.Response:
    return await _request(
        service="files",
        method="POST",
        path="/api/files/validate-ref-audio",
        timeout=timeout,
        data={"path": path},
    )


async def prepare_ref_audio(*, data: Dict[str, Any], timeout: float = 30.0) -> httpx.Response:
    return await _request(
        service="files",
        method="POST",
        path="/api/files/prepare-ref-audio",
        timeout=timeout,
        data=data,
    )


# ========= training API wrappers =========

async def start_training(*, payload: Dict[str, Any], timeout: float = 30.0) -> httpx.Response:
    return await _request(service="training", method="POST", path="/api/train/start", timeout=timeout, json_body=payload)


async def get_training_status(*, model_name: str, timeout: float = 10.0) -> httpx.Response:
    return await _request(service="training", method="GET", path=f"/api/train/status/{model_name}", timeout=timeout)


async def get_training_log(*, model_name: str, timeout: float = 10.0) -> httpx.Response:
    return await _request(service="training", method="GET", path=f"/api/train/log/{model_name}", timeout=timeout)


# ========= tts API wrappers =========

async def call_tts(*, json_body: Dict[str, Any], timeout: Optional[float] = None) -> httpx.Response:
    """
    [역할]
    Server A TTS `POST /tts` 호출 wrapper.

    [왜 여기서 처리하나]
    - `tts_service`/라우터가 URL/timeout/SSL verify 기본값을 몰라도 되게 한다.

    [주의]
    - non-200 해석은 호출자(`tts_service`)에서 `ensure_success_response()` 또는 커스텀 정책으로 처리한다.
    """
    return await _request(
        service="tts",
        method="POST",
        path="/tts",
        timeout=(timeout if timeout is not None else settings.TTS_TIMEOUT),
        json_body=json_body,
    )


async def set_tts_gpt_weights(*, weights_path: str, timeout: float = 10.0) -> httpx.Response:
    return await _request(
        service="tts",
        method="GET",
        path="/set_gpt_weights",
        timeout=timeout,
        params={"weights_path": weights_path},
    )


async def set_tts_sovits_weights(*, weights_path: str, timeout: float = 10.0) -> httpx.Response:
    return await _request(
        service="tts",
        method="GET",
        path="/set_sovits_weights",
        timeout=timeout,
        params={"weights_path": weights_path},
    )
