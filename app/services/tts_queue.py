"""
DEPRECATED: 레거시 Redis Queue helper 모듈.

[현재 상태]
- 이 모듈의 과거 구현(큐 등록/XREAD 스트림 소비)은 사용 중단되었다.
- 실제 TTS 큐/스트림 처리는 `app/workers/tts_worker.py`가 담당한다.

[왜 파일을 남겨두나]
- 과거 문서/참조 링크가 이 경로를 가리킬 수 있어, 1차 리팩토링에서는 경로 자체는 유지한다.
- 다만 주석 처리된 레거시 코드를 그대로 두면 현재 동작과 혼동되므로 명시적 deprecated stub로 교체한다.
"""
from __future__ import annotations


DEPRECATED_TTS_QUEUE_MESSAGE = (
    "app.services.tts_queue is deprecated and no longer provides queue helpers. "
    "Use the current worker-based pipeline in app/workers/tts_worker.py."
)


def raise_deprecated_tts_queue_usage() -> None:
    """
    [역할]
    레거시 TTS queue 모듈 사용 시 즉시 명시적인 오류를 발생시킨다.
    """
    raise RuntimeError(DEPRECATED_TTS_QUEUE_MESSAGE)
