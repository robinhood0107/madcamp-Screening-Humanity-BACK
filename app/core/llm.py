import httpx
import logging
from typing import List, Dict, Any, Optional, AsyncGenerator
import google.generativeai as genai
from app.core.config import settings

logger = logging.getLogger(__name__)

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
    """
    if model is None:
        model = "gemini-2.5-flash" # 기본 모델 (수정됨: 2.0 -> 2.5)

    # Gemini 처리
    if model.startswith("gemini-"):
        api_key = getattr(settings, "GEMINI_API_KEY", None)
        if not api_key:
            raise ValueError("GEMINI_API_KEY가 설정되지 않았습니다.")

        # 사용자가 지정한 모델명 그대로 사용
        real_model = model

        try:
            genai.configure(api_key=api_key)
            
            # Messages에서 system prompt 추출 및 history 구성
            chat_history = []
            
            # system_instruction이 명시되지 않았다면 messages에서 추출
            if not system_instruction:
                system_messages = [msg["content"] for msg in messages if msg["role"] == "system"]
                if system_messages:
                    system_instruction = "\n".join(system_messages)
            
            # User/Assistant 메시지 구성
            for msg in messages:
                role = msg["role"]
                content = msg["content"]
                if role == "user":
                    chat_history.append({"role": "user", "parts": [content]})
                elif role == "assistant":
                    chat_history.append({"role": "model", "parts": [content]})
                # system role은 이미 system_instruction으로 처리됨
            
            # Safety Settings (BLOCK_NONE: 무검열)
            # from google.generativeai.types import HarmCategory, HarmBlockThreshold (ImportError 방지)
            safety_settings = {
                genai.types.HarmCategory.HARM_CATEGORY_HARASSMENT: genai.types.HarmBlockThreshold.BLOCK_NONE,
                genai.types.HarmCategory.HARM_CATEGORY_HATE_SPEECH: genai.types.HarmBlockThreshold.BLOCK_NONE,
                genai.types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: genai.types.HarmBlockThreshold.BLOCK_NONE,
                genai.types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: genai.types.HarmBlockThreshold.BLOCK_NONE,
            }

            # Generation Config
            generation_config = {
                "temperature": temperature,
                "max_output_tokens": max_tokens,
            }
            
            if json_mode:
                generation_config["response_mime_type"] = "application/json"

            # Model 초기화 (system_instruction 적용)
            gemini_model = genai.GenerativeModel(
                model_name=real_model,
                system_instruction=system_instruction
            )
            
            # 비동기 호출
            response = await gemini_model.generate_content_async(
                contents=chat_history,
                generation_config=generation_config,
                safety_settings=safety_settings
            )
            
            return {
                "content": response.text.strip(),
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0
                }
            }

        except Exception as e:
            logger.error(f"Gemini 호출 실패 ({real_model}): {e}")
            raise

    # Ollama 처리
    api_path = getattr(settings, 'OLLAMA_API_PATH', "/api/chat")
    base_url = settings.OLLAMA_BASE_URL.rstrip('/')
    if not api_path.startswith('/'):
        api_path = '/' + api_path
    api_url = f"{base_url}{api_path}"
    
    # Ollama 요청 데이터 구성
    # system_instruction이 있으면 messages의 맨 앞에 system 메시지로 추가
    final_messages = messages.copy()
    if system_instruction:
        # 이미 system 메시지가 있는지 확인하고 병합하거나 추가
        existing_system = next((i for i, m in enumerate(final_messages) if m["role"] == "system"), None)
        if existing_system is not None:
            final_messages[existing_system]["content"] = system_instruction + "\n" + final_messages[existing_system]["content"]
        else:
            final_messages.insert(0, {"role": "system", "content": system_instruction})

    request_data = {
        "model": model,
        "messages": final_messages,
        "stream": False,
    }
    
    if json_mode:
        request_data["format"] = "json"
    
    # 속도 최적화 옵션
    request_data["options"] = {
        "temperature": temperature,
        "num_predict": min(max_tokens, 256),  # 응답 토큰 제한 (속도 ↑)
        "num_ctx": 4096,      # 4K 컨텍스트
        "num_gpu": -1,        # 전체 GPU 레이어 사용
        "num_thread": 8,      # CPU 스레드 수
    }
    request_data["keep_alive"] = -1  # 모델 영구 로드 (콜드스타트 방지)
    
    try:
        ssl_verify = getattr(settings, 'OLLAMA_SSL_VERIFY', False)
        
        async with httpx.AsyncClient(
            timeout=300.0,  # 5분 (첫 모델 로드 시 시간 필요)
            verify=ssl_verify
        ) as client:
            response = await client.post(
                api_url,
                json=request_data,
                headers={"Content-Type": "application/json"}
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
                    "completion_tokens": result.get("eval_count", 0)
                }
            }
    except Exception as e:
        logger.error(f"LLM 호출 실패: {e}")
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
    """
    if model is None:
        model = "gemini-2.5-flash"

    # Gemini 스트리밍 처리
    if model.startswith("gemini-"):
        api_key = getattr(settings, "GEMINI_API_KEY", None)
        if not api_key:
            raise ValueError("GEMINI_API_KEY가 설정되지 않았습니다.")

        real_model = model

        try:
            genai.configure(api_key=api_key)
            
            # Messages에서 system prompt 추출 및 history 구성
            chat_history = []
            
            if not system_instruction:
                system_messages = [msg["content"] for msg in messages if msg["role"] == "system"]
                if system_messages:
                    system_instruction = "\n".join(system_messages)
            
            # User/Assistant 메시지 구성
            for msg in messages:
                role = msg["role"]
                content = msg["content"]
                if role == "user":
                    chat_history.append({"role": "user", "parts": [content]})
                elif role == "assistant":
                    chat_history.append({"role": "model", "parts": [content]})
            
            # Safety Settings (BLOCK_NONE)
            safety_settings = {
                genai.types.HarmCategory.HARM_CATEGORY_HARASSMENT: genai.types.HarmBlockThreshold.BLOCK_NONE,
                genai.types.HarmCategory.HARM_CATEGORY_HATE_SPEECH: genai.types.HarmBlockThreshold.BLOCK_NONE,
                genai.types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: genai.types.HarmBlockThreshold.BLOCK_NONE,
                genai.types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: genai.types.HarmBlockThreshold.BLOCK_NONE,
            }

            # Generation Config
            generation_config = {
                "temperature": temperature,
                "max_output_tokens": max_tokens,
            }

            # Model 초기화
            gemini_model = genai.GenerativeModel(
                model_name=real_model,
                system_instruction=system_instruction
            )
            
            # 스트리밍 호출 (동기 generate_content + stream=True 사용)
            # Note: google-generativeai의 비동기 스트리밍은 제한적이므로 동기 스트림 사용
            response = gemini_model.generate_content(
                contents=chat_history,
                generation_config=generation_config,
                safety_settings=safety_settings,
                stream=True  # 스트리밍 활성화
            )
            
            # 각 청크를 yield
            for chunk in response:
                if chunk.text:
                    yield chunk.text

        except Exception as e:
            logger.error(f"Gemini 스트리밍 호출 실패 ({real_model}): {e}")
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
        yield result["content"]

