"""
스토리 생성(`/api/generate/story`) 라우터에서 재사용 가능한 공통 로직 모음.

[의도]
- `app/api/ai.py::generate_story`의 긴 함수에서 프롬프트 생성, 모델 선택, 결과 파싱, Mock 생성을 분리한다.
- 1차는 외부 응답 shape/에러 정책을 유지하고, 반복/혼합 책임만 줄인다.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status

from app.core.config import settings
from app.core.llm import call_llm

logger = logging.getLogger(__name__)


def build_story_generation_system_prompt(*, user_name: str, opponent_name: str) -> str:
    """
    [역할]
    스토리 생성용 system prompt를 현재 정책에 맞게 생성한다.

    [왜 여기서 처리하나]
    - 라우터 본문에 긴 문자열이 그대로 있으면 모델 선택/에러 처리 로직을 읽기 어렵다.
    - 프롬프트 문구 수정 시 라우터 동작 코드와 섞이지 않게 분리해 두는 편이 유지보수에 유리하다.

    [입력/출력]
    - 입력: 사용자 이름, 상대 캐릭터 이름
    - 출력: story generation system prompt 문자열

    [부작용]
    - 없음 (순수 문자열 조합)

    [실패/예외]
    - 예외 없음

    [주의]
    - 출력 형식 지침(`[줄거리]`, `[배경]`)은 아래 파서와 사실상 계약 관계라 임의 변경하면 파싱 회귀가 난다.
    """
    return (
        "당신은 드라마와 영화의 전문 시나리오 작가입니다. "
        "사용자가 제공한 상황과 인물 정보를 바탕으로, 몰입감 있고 드라마틱한 줄거리(Plot)와 배경(Background)을 **빠르게** 작성해주세요.\n\n"
        "[지침]\n"
        f"1. 반드시 주어진 인물 이름인 '{user_name}'(나/주인공)와 '{opponent_name}'(상대역)만 사용하세요.\n"
        "2. 전체적인 분위기는 선택한 상황에 맞추되, 인물들 간의 갈등이나 감정이 잘 드러나도록 풍부하게 묘사하세요.\n"
        "3. 문체는 소설처럼 몰입감 있게 서술하고, 최대 5줄의 한국어로 작성하세요.\n"
        "4. 줄거리의 마지막 문장은 두 사람이 대화를 시작하기 직전의 긴장감 있는 상황 묘사로 끝내세요.\n"
        "5. **JSON 형식을 사용하지 마세요.** 아래 형식을 지켜 줄글로 작성하세요.\n\n"
        "[출력 형식]\n"
        "[줄거리]\n"
        "(여기에 줄거리 내용을 작성하세요)\n\n"
        "[배경]\n"
        "(여기에 장소, 시간, 분위기 등을 묘사한 배경 설명을 작성하세요)"
    )


def build_story_generation_messages(*, system_prompt: str, situation: str) -> List[Dict[str, str]]:
    """
    [역할]
    스토리 생성 LLM 호출용 메시지 페이로드를 구성한다.

    [왜 여기서 처리하나]
    - 라우터에서 메시지 shape를 반복 생성하는 코드가 다른 story/ai 경로로 퍼질 가능성이 있다.
    - 프롬프트와 user input 페이로드 구성을 함수로 고정해 두면 테스트하기 쉽다.

    [입력/출력]
    - 입력: system prompt, 상황 문자열
    - 출력: `call_llm` 입력용 messages 리스트

    [부작용]
    - 없음

    [실패/예외]
    - 예외 없음

    [주의]
    - user 메시지 포맷 `상황: ...` 문구는 프롬프트 성능에 영향을 줄 수 있어 1차에서 유지한다.
    """
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"상황: {situation}"},
    ]


def ensure_story_auto_mode_gemini_key() -> None:
    """
    [역할]
    스토리 생성에서 모델 미지정(auto mode)일 때 Gemini API 키 존재 여부를 검증한다.

    [왜 여기서 처리하나]
    - 기존 라우터는 auto mode에서 Gemini를 1차 시도로 강제하고 키가 없으면 503을 반환한다.
    - 이 정책은 단순 설정 체크지만 사용자 노출 에러 계약과 연결돼 있으므로 별도 함수로 분리해 명시한다.

    [입력/출력]
    - 입력/출력 없음 (조건 불만족 시 예외)

    [부작용]
    - 없음

    [실패/예외]
    - `HTTPException(503)` 발생 가능

    [주의]
    - 여기서 로컬 LLM로 바로 폴백하지 않는다. 기존 정책(키 미설정은 운영 설정 오류로 간주)을 유지한다.
    """
    if not getattr(settings, "GEMINI_API_KEY", None):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Gemini API Key가 설정되지 않았습니다. Backend .env의 "
                "GEMINI_API_KEY(또는 GOOGLE_API_KEY legacy alias)를 확인해주세요."
            ),
        )


async def call_story_llm_with_selection(
    *,
    messages: List[Dict[str, str]],
    user_selected_model: Optional[str],
    temperature: float,
    max_tokens: int,
    logger_hint: str = "Story generation",
) -> str:
    """
    [역할]
    스토리 생성용 LLM 호출을 수행한다. (사용자 지정 모델 우선 / auto mode는 Gemini→로컬 폴백)

    [왜 여기서 처리하나]
    - `generate_story`에서 모델 선택 분기 + Gemini 실패 폴백 + 로깅이 길어져 핵심 응답 조립 로직을 가린다.
    - 모델 선택 정책을 한 곳에 모아두면 story 경로에서 같은 규칙을 재사용할 수 있다.

    [입력/출력]
    - 입력: messages, 선택 모델, 샘플링/토큰 옵션
    - 출력: LLM 응답 텍스트(content)

    [부작용]
    - 외부 LLM HTTP 호출, 로깅

    [실패/예외]
    - `call_llm` 예외를 그대로 전파할 수 있다.
    - auto mode 키 미설정은 `HTTPException(503)`를 발생시킨다.

    [주의]
    - auto mode에서 Gemini 1차 시도 후 실패하면 로컬 `gemma-3-27b`로 폴백하는 기존 정책 유지.
    """
    if user_selected_model:
        logger.info("%s: using user-selected model %s", logger_hint, user_selected_model)
        result = await call_llm(
            messages,
            model=user_selected_model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=False,
        )
        return result if isinstance(result, str) else result.get("content", "")

    ensure_story_auto_mode_gemini_key()
    gemini_model = getattr(settings, "GOOGLE_API_MODEL", None) or "gemini-2.5-flash"

    try:
        result = await call_llm(
            messages,
            model=gemini_model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=False,
        )
    except Exception as gemini_error:
        logger.warning(
            "%s: Gemini failed, switching to Local Ollama (gemma-3-27b): %s",
            logger_hint,
            gemini_error,
        )
        result = await call_llm(
            messages,
            model="gemma-3-27b",
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=False,
        )

    return result if isinstance(result, str) else result.get("content", "")


def parse_story_sections(content: str) -> Tuple[str, str]:
    """
    [역할]
    `[줄거리]`, `[배경]` 태그 기반 응답을 `plot/background`로 파싱한다.

    [왜 여기서 처리하나]
    - 스토리 생성 응답 파싱 로직은 분기/예외가 많고 라우터 본문을 길게 만든다.
    - parser를 분리하면 LLM 모델별 출력 흔들림을 테스트/보정하기 쉬워진다.

    [입력/출력]
    - 입력: LLM 응답 텍스트
    - 출력: `(plot, background)` 튜플

    [부작용]
    - 없음

    [실패/예외]
    - 내부 파싱 예외는 잡아서 `plot=전체텍스트` 폴백으로 복구한다 (기존 동작 유지)

    [주의]
    - 태그 길이 슬라이싱 오프셋(`[줄거리]` 5, `[배경]` 4)은 기존 구현과 동일하게 유지.
    """
    plot = ""
    background = ""
    text = (content or "")

    try:
        plot_idx = text.find("[줄거리]")
        bg_idx = text.find("[배경]")

        if plot_idx != -1 and bg_idx != -1:
            if plot_idx < bg_idx:
                plot = text[plot_idx + 5 : bg_idx].strip()
                background = text[bg_idx + 4 :].strip()
            else:
                background = text[bg_idx + 4 : plot_idx].strip()
                plot = text[plot_idx + 5 :].strip()
        elif plot_idx != -1:
            plot = text[plot_idx + 5 :].strip()
        elif bg_idx != -1:
            background = text[:bg_idx].strip()
            plot = text[bg_idx + 4 :].strip()
        else:
            plot = text.strip()
    except Exception as e:
        logger.warning("Story section parsing failed, fallback to full plot text: %s", e)
        plot = text.strip()
        background = ""

    return plot, background


def build_story_mock_plot(situation: str) -> str:
    """
    [역할]
    스토리 생성 실패 시 반환할 테스트/폴백용 줄거리를 생성한다.

    [왜 여기서 처리하나]
    - Mock 응답 문구도 사용자 체감 UX에 영향이 크고 라우터 예외처리 길이를 늘린다.
    - 추후 환경별(개발/운영) fallback 메시지 정책을 분기할 때 한 곳에서 관리하기 쉽다.

    [입력/출력]
    - 입력: 사용자 상황 문자열
    - 출력: mock plot 문자열

    [부작용]
    - 없음

    [실패/예외]
    - 예외 없음

    [주의]
    - 1차 리팩토링에서는 기존 문구를 최대한 유지한다.
    """
    return (
        f"운명의 장난처럼 {situation} 상황이 펼쳐집니다. "
        "서로의 오해와 감정이 얽히며 예상치 못한 전개가 시작되려 합니다. "
        "이 긴장감 넘치는 순간, 당신의 선택이 모든 것을 결정할 것입니다."
    )
