import httpx
import logging
from typing import List, Dict, Any, Optional, AsyncGenerator
import google.generativeai as genai
from app.core.config import settings

logger = logging.getLogger(__name__)


def _resolve_gemini_harm_block_threshold():
    """
    Gemini safety threshold를 env(settings.GOOGLE_SAFETY_THRESHOLD) 문자열에서 enum으로 변환.
    유효하지 않은 값이면 기존 동작과 호환되도록 BLOCK_NONE으로 폴백한다.
    """
    raw = (getattr(settings, "GOOGLE_SAFETY_THRESHOLD", "") or "BLOCK_NONE").strip().upper()
    threshold_enum = getattr(genai.types.HarmBlockThreshold, raw, None)
    if threshold_enum is None:
        logger.warning(
            "Invalid GOOGLE_SAFETY_THRESHOLD=%r. Falling back to BLOCK_NONE. "
            "Allowed: BLOCK_NONE, BLOCK_ONLY_HIGH, BLOCK_MEDIUM_AND_ABOVE, BLOCK_LOW_AND_ABOVE, HARM_BLOCK_THRESHOLD_UNSPECIFIED",
            raw,
        )
        threshold_enum = genai.types.HarmBlockThreshold.BLOCK_NONE
    return threshold_enum


def _resolve_default_gemini_model() -> str:
    """
    env(settings.GOOGLE_API_MODEL)에서 Gemini 기본 모델명을 가져온다.
    비어 있으면 기존 기본값(gemini-2.5-flash)으로 폴백한다.
    """
    model = (getattr(settings, "GOOGLE_API_MODEL", "") or "").strip()
    return model or "gemini-2.5-flash"


def _build_gemini_safety_settings() -> Dict[Any, Any]:
    threshold = _resolve_gemini_harm_block_threshold()
    return {
        genai.types.HarmCategory.HARM_CATEGORY_HARASSMENT: threshold,
        genai.types.HarmCategory.HARM_CATEGORY_HATE_SPEECH: threshold,
        genai.types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: threshold,
        genai.types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: threshold,
    }


def _extract_system_instruction_from_messages(
    messages: List[Dict[str, str]],
    explicit_system_instruction: Optional[str],
) -> Optional[str]:
    """
    system instruction이 명시되지 않았으면 messages의 system role을 합쳐서 사용한다.

    [왜 여기서 처리하나]
    - Gemini/Ollama 경로 모두 "명시 파라미터 우선, 없으면 messages의 system role 병합" 규칙이 필요하다.
    - 이 규칙이 분산되면 provider별 프롬프트 차이로 회귀가 생기기 쉬워 공통 helper로 고정한다.

    [주의]
    - explicit_system_instruction가 있으면 messages의 system role보다 우선한다. (기존 API 계약)
    """
    if explicit_system_instruction:
        return explicit_system_instruction
    system_messages = [msg["content"] for msg in messages if msg.get("role") == "system"]
    if system_messages:
        return "\n".join(system_messages)
    return None


def _build_gemini_chat_history(messages: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """
    `call_llm`/`call_llm_stream`에서 공통으로 쓰는 Gemini contents payload를 생성한다.

    [왜 여기서 처리하나]
    - Gemini SDK role 매핑(`assistant` -> `model`) 규칙을 한 곳에 고정해야 non-stream/stream 동작이 일치한다.

    [주의]
    - system role은 여기서 넣지 않고 `system_instruction`으로만 처리한다.
      중복 주입되면 프롬프트가 과하게 강화되어 응답 품질이 흔들릴 수 있다.
    """
    chat_history: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "user":
            chat_history.append({"role": "user", "parts": [content]})
        elif role == "assistant":
            chat_history.append({"role": "model", "parts": [content]})
        # system role은 system_instruction으로만 처리
    return chat_history


def _normalize_llm_result(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        return result
    return {
        "content": str(result or "").strip(),
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
    }


async def _call_gemini_non_stream(
    *,
    messages: List[Dict[str, str]],
    model: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
    system_instruction: Optional[str],
) -> Dict[str, Any]:
    """
    [역할]
    Gemini 비스트리밍 호출을 수행하고 공통 결과 shape(`content`, `usage`)로 반환한다.

    [왜 여기서 처리하나]
    - Gemini SDK 설정/API 호출/응답 파싱을 `call_llm` 본문에서 분리해 provider dispatch를 읽기 쉽게 유지한다.

    [부작용]
    - Gemini SDK 전역 configure
    - 외부 네트워크 호출

    [실패/예외]
    - API 키 누락/SDK 호출 실패/응답 파싱 오류를 그대로 상위로 전파한다.
      (provider별 로그/최종 예외 정책은 `call_llm` 담당)

    [주의]
    - 반환 shape는 Ollama 경로와 맞춰서 상위 라우터들이 provider 차이를 덜 의식하게 한다.
    """
    api_key = getattr(settings, "GEMINI_API_KEY", None)
    if not api_key:
        raise ValueError("GEMINI_API_KEY가 설정되지 않았습니다.")

    genai.configure(api_key=api_key)
    resolved_system_instruction = _extract_system_instruction_from_messages(messages, system_instruction)
    chat_history = _build_gemini_chat_history(messages)
    safety_settings = _build_gemini_safety_settings()

    generation_config = {
        "temperature": temperature,
        "max_output_tokens": max_tokens,
    }
    if json_mode:
        generation_config["response_mime_type"] = "application/json"

    gemini_model = genai.GenerativeModel(
        model_name=model,
        system_instruction=resolved_system_instruction,
    )
    response = await gemini_model.generate_content_async(
        contents=chat_history,
        generation_config=generation_config,
        safety_settings=safety_settings,
    )
    return {
        "content": response.text.strip(),
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
    }


def _merge_system_instruction_for_ollama_messages(
    messages: List[Dict[str, str]],
    system_instruction: Optional[str],
) -> List[Dict[str, str]]:
    """
    [역할]
    Ollama용 메시지 목록에 system instruction을 기존 규칙대로 병합한다.

    [왜 여기서 처리하나]
    - Ollama는 Gemini처럼 별도 `system_instruction` 파라미터가 없어서 메시지 배열 병합 정책이 필요하다.
    - non-stream/stream(간접 호출)에서 같은 병합 규칙을 재사용할 수 있게 분리한다.

    [주의]
    - 기존 system 메시지가 있으면 prepend가 아니라 내용을 합친다. (1차 호환성 유지)
    """
    final_messages = messages.copy()
    if not system_instruction:
        return final_messages

    existing_system = next((i for i, m in enumerate(final_messages) if m["role"] == "system"), None)
    if existing_system is not None:
        final_messages[existing_system]["content"] = (
            system_instruction + "\n" + final_messages[existing_system]["content"]
        )
    else:
        final_messages.insert(0, {"role": "system", "content": system_instruction})
    return final_messages


def _build_ollama_request_data(
    *,
    model: str,
    messages: List[Dict[str, str]],
    temperature: float,
    max_tokens: int,
    json_mode: bool,
    system_instruction: Optional[str],
) -> Dict[str, Any]:
    """
    [역할]
    Ollama `/api/chat` 요청 payload를 공통 옵션 정책으로 생성한다.

    [왜 여기서 처리하나]
    - 모델/temperature/max_tokens/json_mode/system 병합 규칙이 분산되면 provider 회귀 추적이 어렵다.
    - 요청 payload를 helper로 분리해 `call_llm`/`_call_ollama_non_stream`의 책임을 줄인다.

    [주의]
    - `num_predict`, `num_ctx` 등 옵션 값은 운영 성능/품질 절충값이다. 1차 리팩토링에서 의미 변경 금지.
    """
    final_messages = _merge_system_instruction_for_ollama_messages(messages, system_instruction)
    request_data: Dict[str, Any] = {
        "model": model,
        "messages": final_messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": min(max_tokens, 256),
            "num_ctx": 4096,
            "num_gpu": -1,
            "num_thread": 8,
        },
        "keep_alive": -1,
    }
    if json_mode:
        request_data["format"] = "json"
    return request_data


async def _call_ollama_non_stream(
    *,
    messages: List[Dict[str, str]],
    model: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
    system_instruction: Optional[str],
) -> Dict[str, Any]:
    """
    [역할]
    Ollama 비스트리밍 호출을 수행하고 공통 결과 shape(`content`, `usage`)로 반환한다.

    [왜 여기서 처리하나]
    - URL 조합, SSL verify, httpx 호출, 응답 파싱을 provider helper로 묶어 `call_llm` dispatch를 단순화한다.

    [부작용]
    - 외부 HTTP 호출 (`settings.OLLAMA_BASE_URL`)

    [실패/예외]
    - httpx 오류/상태코드 오류/응답 shape 불일치(`message` 없음) 예외를 상위로 전파한다.

    [주의]
    - `OLLAMA_API_PATH`/SSL verify 설정은 운영 환경별 편차가 큰 포인트라 로그/문서와 함께 관리한다.
    """
    api_path = getattr(settings, "OLLAMA_API_PATH", "/api/chat")
    base_url = settings.OLLAMA_BASE_URL.rstrip("/")
    if not api_path.startswith("/"):
        api_path = "/" + api_path
    api_url = f"{base_url}{api_path}"

    request_data = _build_ollama_request_data(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
        system_instruction=system_instruction,
    )
    ssl_verify = getattr(settings, "OLLAMA_SSL_VERIFY", False)

    async with httpx.AsyncClient(
        timeout=300.0,
        verify=ssl_verify,
    ) as client:
        response = await client.post(
            api_url,
            json=request_data,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        result = response.json()

    if "message" not in result:
        raise KeyError("Ollama 응답에 'message' 필드가 없습니다.")

    message = result["message"]
    content = message.get("content", "(응답이 비어있습니다.)")
    return {
        "content": content.strip(),
        "usage": {
            "prompt_tokens": result.get("prompt_eval_count", 0),
            "completion_tokens": result.get("eval_count", 0),
        },
    }

async def call_llm(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 512,
    json_mode: bool = False,
    system_instruction: Optional[str] = None
) -> Dict[str, Any]:
    """
    공통 LLM 호출 함수 (Ollama 및 Gemini 지원)
    json_mode: True일 경우 Gemini의 JSON 모드 활성화 (response_mime_type="application/json")
    system_instruction: Gemini 시스템 프롬프트 (messages의 system role보다 우선함)

    [왜 여기서 처리하나]
    - 라우터/서비스 계층은 provider별 SDK/httpx 차이를 몰라도 되게 하고,
      공통 입력/출력 shape와 최소 dispatch 규칙만 의존하도록 만든다.

    [실패/예외]
    - provider helper에서 올라온 예외를 provider 문맥 로그만 추가하고 다시 올린다.
      최종 사용자 응답 정책(HTTPException 변환/Mock fallback)은 라우터 책임이다.

    [주의]
    - provider 선택 규칙은 현재 `model.startswith("gemini-")` 기반이다.
      모델명 네이밍 정책을 바꾸면 호출 경로가 바뀌므로 문서/환경 설정도 같이 점검해야 한다.
    """
    if model is None:
        model = _resolve_default_gemini_model()  # env(GOOGLE_API_MODEL)로 조정 가능

    if model.startswith("gemini-"):
        try:
            return await _call_gemini_non_stream(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
                system_instruction=system_instruction,
            )
        except Exception as e:
            logger.error("Gemini 호출 실패 (%s): %s", model, e)
            raise

    try:
        return await _call_ollama_non_stream(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            system_instruction=system_instruction,
        )
    except Exception as e:
        logger.error("LLM 호출 실패: %s", e)
        raise


async def call_llm_stream(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 512,
    system_instruction: Optional[str] = None
) -> AsyncGenerator[str, None]:
    """
    Gemini 스트리밍 LLM 호출 함수.
    각 응답 청크를 yield하여 SSE 전송 가능하게 함.
    Ollama는 스트리밍 미지원 → 전체 응답을 한 번에 yield.

    [왜 여기서 처리하나]
    - `/chat/stream` 같은 상위 라우터가 provider별 스트리밍 구현 차이를 직접 다루지 않도록 숨긴다.
    - 비스트리밍 경로와 같은 입력 인터페이스를 유지해 호출자 코드를 단순화한다.

    [실패/예외]
    - Gemini 스트리밍 예외는 로그 후 재전파한다. SSE 에러 프레임 변환은 상위(`chat.py`)에서 담당한다.

    [주의]
    - Ollama는 현재 비스트리밍 fallback으로 한 번에 yield한다. 청크 단위 동작을 기대하는 호출자는 이 차이를 고려해야 한다.
    """
    if model is None:
        model = _resolve_default_gemini_model()

    # Gemini 스트리밍 처리
    if model.startswith("gemini-"):
        api_key = getattr(settings, "GEMINI_API_KEY", None)
        if not api_key:
            raise ValueError("GEMINI_API_KEY가 설정되지 않았습니다.")

        try:
            genai.configure(api_key=api_key)
            resolved_system_instruction = _extract_system_instruction_from_messages(messages, system_instruction)
            chat_history = _build_gemini_chat_history(messages)
            safety_settings = _build_gemini_safety_settings()
            generation_config = {
                "temperature": temperature,
                "max_output_tokens": max_tokens,
            }
            gemini_model = genai.GenerativeModel(
                model_name=model,
                system_instruction=resolved_system_instruction,
            )
            response = gemini_model.generate_content(
                contents=chat_history,
                generation_config=generation_config,
                safety_settings=safety_settings,
                stream=True,
            )
            for chunk in response:
                if chunk.text:
                    yield chunk.text
        except Exception as e:
            logger.error("Gemini 스트리밍 호출 실패 (%s): %s", model, e)
            raise

    else:
        # Ollama: 스트리밍 미지원 → 전체 응답을 한 번에 yield
        result = await call_llm(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            system_instruction=system_instruction
        )
        normalized = _normalize_llm_result(result)
        yield normalized["content"]

