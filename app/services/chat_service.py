"""
채팅 라우터(`app/api/chat.py`)에서 재사용 가능한 공통 로직 모음.

[의도]
- 1차/Phase C에서는 라우터의 외부 계약(path/응답 shape)을 유지하면서,
  순수 함수/반복 로직/조회 보조 로직만 서비스 계층으로 이동한다.
- 특히 `chat()`와 `chat_stream()`에 중복된 시나리오 추출, 시스템 프롬프트 분리,
  응답 후처리(이모티콘/이름 접두사 제거)를 한 곳으로 모은다.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.character import Character
from app.models.chat_message import ChatMessage


def extract_scenario_fields(
    scenario: Optional[Dict[str, str]],
    *,
    default_user_name: str,
) -> Dict[str, Optional[str]]:
    """
    [역할]
    request.scenario에서 채팅에 필요한 필드를 기본값과 함께 추출한다.

    [왜 여기서 처리하나]
    - `chat()`와 `chat_stream()`가 같은 키(`opponent`, `situation`, `user_name`, `background`)를 반복 추출한다.
    - 기본값 정책이 흩어지면 프롬프트/응답이 미세하게 달라져 디버깅이 어려워진다.

    [입력/출력]
    - 입력: optional scenario dict, 기본 user_name
    - 출력: 표준 키를 가진 dict (`opponent`, `situation`, `user_name`, `background`)

    [부작용]
    - 없음 (순수 함수)

    [실패/예외]
    - 예외 없음. 값이 없으면 기본값으로 채운다.

    [주의]
    - 기본값 문구는 프롬프트 품질/테스트 스냅샷에 영향을 줄 수 있으므로 라우터마다 임의 변경하지 않는다.
    """
    scenario = scenario or {}
    return {
        "opponent": scenario.get("opponent", "상대방"),
        "situation": scenario.get("situation", "대화 중"),
        "user_name": scenario.get("user_name", default_user_name),
        "background": scenario.get("background"),
    }


async def resolve_character_identity(
    *,
    character_id: Optional[str],
    fallback_name: str,
    db: AsyncSession,
    preset_loader: Callable[[], List[Dict[str, Any]]],
) -> Tuple[str, Optional[str]]:
    """
    [역할]
    캐릭터 ID를 기준으로 실제 캐릭터 이름(및 DB 페르소나 override 후보)을 해석한다.

    [왜 여기서 처리하나]
    - `chat()`와 `chat_stream()` 모두 Preset JSON → DB 순서로 이름을 해석하는 중복 로직이 있다.
    - 여기로 모으면 캐릭터 표시명 결정 정책이 한 곳에서 관리된다.

    [입력/출력]
    - 입력: character_id, 기본 이름(fallback), DB 세션, preset loader 함수
    - 출력: `(resolved_name, persona_override)` 튜플
      - `persona_override`는 DB 캐릭터에서 가져온 페르소나(없으면 None)

    [부작용]
    - DB 조회 1회 가능
    - preset loader 호출(파일 읽기 캐시 포함 가능)

    [실패/예외]
    - DB 오류 등 예외는 상위 라우터로 올린다 (라우터가 전체 API 정책에 맞게 처리).

    [주의]
    - Preset → DB 우선순위는 기존 동작 호환성 때문에 유지한다.
    """
    if not character_id:
        return fallback_name, None

    resolved_name = fallback_name
    persona_override: Optional[str] = None

    presets = preset_loader()
    preset = next((x for x in presets if x.get("id") == character_id), None)
    if preset:
        return preset.get("name", resolved_name), None

    result = await db.execute(select(Character).where(Character.id == character_id))
    character = result.scalar_one_or_none()
    if character:
        resolved_name = character.name
        if character.persona:
            persona_override = character.persona

    return resolved_name, persona_override


def split_system_and_chat_messages(
    messages: List[Dict[str, str]],
) -> Tuple[Optional[str], List[Dict[str, str]]]:
    """
    [역할]
    LLM 입력 메시지 리스트에서 system 메시지를 합치고 나머지 chat 메시지를 분리한다.

    [왜 여기서 처리하나]
    - `chat()`와 `chat_stream()`에 동일한 분리 루프가 있다.
    - 시스템 프롬프트 합치기 규칙이 바뀌면 두 엔드포인트가 같이 바뀌어야 하므로 공통화가 유리하다.

    [입력/출력]
    - 입력: `{"role","content"}` 형태 메시지 리스트
    - 출력: `(system_instruction, chat_messages)`

    [부작용]
    - 없음 (새 리스트 생성)

    [실패/예외]
    - 메시지 shape가 깨졌으면 KeyError 가능 (호출자 데이터 계약 위반)

    [주의]
    - system 메시지가 여러 개면 기존 동작대로 `\\n\\n`로 이어 붙인다.
    """
    system_instruction: Optional[str] = None
    chat_messages: List[Dict[str, str]] = []

    for msg in messages:
        if msg["role"] == "system":
            if system_instruction:
                system_instruction += "\n\n" + msg["content"]
            else:
                system_instruction = msg["content"]
        else:
            chat_messages.append(msg)

    return system_instruction, chat_messages


def sanitize_assistant_reply_content(content: str, speaker_name: Optional[str]) -> str:
    """
    [역할]
    LLM 응답 텍스트에서 이모티콘과 화자 이름 접두사를 제거한다.

    [왜 여기서 처리하나]
    - `/chat` 후처리 규칙은 프론트 표시/TTS 품질에 직접 영향을 주는 계약성 로직이다.
    - 라우터 내부에 두면 다른 엔드포인트로 재사용하기 어렵고, 정규식 버그가 재발하기 쉽다.

    [입력/출력]
    - 입력: raw 응답 텍스트, 화자 이름(선택)
    - 출력: 후처리된 텍스트

    [부작용]
    - 없음 (문자열 가공)

    [실패/예외]
    - 예외 없음. 이름이 없으면 이름 접두사 제거는 건너뛴다.

    [주의]
    - 이름 접두사 제거 정규식은 raw f-string을 사용한다(과거 `\\s` SyntaxWarning 재발 방지).
    """
    cleaned = re.sub(r"[\U00010000-\U0010ffff]", "", content or "")
    if speaker_name:
        safe_name = re.escape(speaker_name)
        cleaned = re.sub(rf"^{safe_name}\s*[:：]\s*", "", cleaned)
    return cleaned


def parse_director_turn_context(
    *,
    persona: str,
    messages: List[Any],
    current_speaker_override: Optional[str],
) -> Dict[str, str]:
    """
    [역할]
    감독 모드 persona 문자열을 파싱해 이번 턴 화자/상대/페르소나를 계산한다.

    [왜 여기서 처리하나]
    - `chat.py::chat` 내부에서 감독 모드 파싱/교대 화자 계산 블록이 길고, 라우터 흐름을 읽기 어렵게 만든다.
    - 파싱 규칙과 턴 선택 규칙을 서비스로 모아두면 `/chat`과 향후 감독 모드 확장 경로가 같은 정책을 공유할 수 있다.

    [입력/출력]
    - 입력: 감독 모드 persona 문자열, 현재까지 메시지 목록, 강제 화자 지정값(선택)
    - 출력: `current_speaker`, `speaker_persona`, `partner_name`, `char1_name`, `char2_name`가 담긴 dict

    [부작용]
    - 없음 (순수 파싱/계산)

    [실패/예외]
    - persona 포맷이 예상과 다르면 `ValueError`를 발생시킨다 (라우터에서 감독 모드 -> 주연 모드 폴백 정책 처리).

    [주의]
    - 교대 규칙은 기존 호환성 유지: AI 메시지 개수 짝수면 배우1, 홀수면 배우2.
    """
    parts = persona.split("\n\n")
    char1_section = parts[0] if len(parts) > 0 else ""
    char2_section = parts[1] if len(parts) > 1 else ""

    if "[배우 1:" not in char1_section or "[배우 2:" not in char2_section:
        raise ValueError("director persona format mismatch")

    char1_name = char1_section.split("[배우 1: ", 1)[1].split("]", 1)[0]
    char2_name = char2_section.split("[배우 2: ", 1)[1].split("]", 1)[0]

    char1_persona = "\n".join(char1_section.split("\n")[1:]) if "\n" in char1_section else char1_section
    char2_persona = "\n".join(char2_section.split("\n")[1:]) if "\n" in char2_section else char2_section

    ai_msg_count = len([m for m in messages if getattr(m, "role", None) in ["assistant", "ai"]])
    if current_speaker_override:
        current_speaker = current_speaker_override
    else:
        current_speaker = char1_name if ai_msg_count % 2 == 0 else char2_name

    if current_speaker == char1_name:
        speaker_persona = char1_persona
        partner_name = char2_name
    else:
        speaker_persona = char2_persona
        partner_name = char1_name

    return {
        "current_speaker": current_speaker,
        "speaker_persona": speaker_persona,
        "partner_name": partner_name,
        "char1_name": char1_name,
        "char2_name": char2_name,
    }


def normalize_request_messages_for_context(messages: List[Any]) -> List[Dict[str, str]]:
    """
    [역할]
    라우터 입력 메시지(Pydantic/DTO)를 컨텍스트 관리 및 LLM 입력용 dict 리스트로 정규화한다.

    [왜 여기서 처리하나]
    - `chat()`와 향후 재사용 경로에서 같은 `"ai" -> "assistant"` 변환과 `role/content` 추출이 반복된다.
    - 정규화 규칙이 흩어지면 context_manager 입력과 실제 LLM 입력이 미세하게 달라질 수 있다.

    [입력/출력]
    - 입력: `request.messages` 같은 message 객체 리스트 (`role`, `content` 속성 기대)
    - 출력: `[{\"role\",\"content\"}, ...]` 리스트

    [부작용]
    - 없음 (새 리스트 생성)

    [실패/예외]
    - 요소가 예상 속성을 가지지 않으면 `AttributeError` 가능 (호출자 계약 위반)

    [주의]
    - role 변환 규칙(`ai` -> `assistant`)은 기존 채팅 저장/LLM 호출 호환성 때문에 유지한다.
    """
    return [
        {
            "role": "assistant" if getattr(m, "role", None) == "ai" else getattr(m, "role", "user"),
            "content": getattr(m, "content", ""),
        }
        for m in messages
    ]


def append_history_messages(
    *,
    messages_acc: List[Dict[str, str]],
    request_messages: List[Dict[str, str]],
    default_user_message: str = "대화를 시작해주세요.",
) -> None:
    """
    [역할]
    정규화된 과거 대화 메시지를 LLM 입력 메시지 리스트에 추가한다. (비어 있으면 기본 유저 메시지 삽입)

    [왜 여기서 처리하나]
    - `chat.py`에서 비어있는 대화 처리 + `assistant/user` 메시지 추가 루프가 오케스트레이션 본문에 섞여 있다.
    - 이 블록은 정책성(default 첫 사용자 프롬프트)과 메시지 매핑만 담당하므로 서비스로 분리하기 좋다.

    [입력/출력]
    - 입력: 누적 메시지 리스트, 정규화된 request_messages, 기본 유저 메시지 문자열
    - 출력: 없음 (messages_acc를 in-place 수정)

    [부작용]
    - `messages_acc` / `request_messages` in-place 수정 (`request_messages`가 비어있으면 기본 메시지를 append)

    [실패/예외]
    - 메시지 shape가 깨지면 KeyError 가능 (호출 계약 위반)

    [주의]
    - 기존 동작 호환성 때문에 `request_messages`가 비어 있으면 기본 유저 메시지를 주입한다.
    """
    if not request_messages:
        request_messages.append({"role": "user", "content": default_user_message})

    for msg in request_messages:
        role = "assistant" if msg["role"] == "ai" else msg["role"]
        messages_acc.append({"role": role, "content": msg["content"]})


async def persist_chat_turn_messages(
    *,
    db: AsyncSession,
    session_id: str,
    raw_request_messages: List[Any],
    assistant_content: str,
    speaker_name: Optional[str],
) -> None:
    """
    [역할]
    채팅 한 턴의 사용자 메시지(마지막 user/human)와 AI 응답을 DB에 저장하고 commit한다.

    [왜 여기서 처리하나]
    - `chat.py::chat` 본문에서 메시지 저장/commit 블록이 길고, LLM 호출/후처리/TTS 흐름을 가린다.
    - 저장 정책(마지막 user만 저장 + assistant 저장 + commit)을 한 곳에 모아두면 회귀 점검이 쉬워진다.

    [입력/출력]
    - 입력: DB 세션, session_id, 원본 request 메시지 객체 리스트, assistant 응답 텍스트, speaker_name
    - 출력: 없음

    [부작용]
    - DB insert 1~2건 + commit

    [실패/예외]
    - DB 세션/commit 오류는 상위 라우터로 전파 (라우터가 전체 API 실패 정책을 가진다)

    [주의]
    - 기존 호환성 유지:
      - 마지막 메시지가 user/human일 때만 사용자 메시지 저장
      - AI 메시지는 항상 `role='assistant'`
      - user 메시지에 `character_name`은 저장하지 않음
    """
    if raw_request_messages and getattr(raw_request_messages[-1], "role", None) in ["user", "human"]:
        last_msg = raw_request_messages[-1]
        db.add(
            ChatMessage(
                session_id=session_id,
                role="user",
                content=getattr(last_msg, "content", ""),
            )
        )

    db.add(
        ChatMessage(
            session_id=session_id,
            role="assistant",
            content=assistant_content,
            character_name=speaker_name,
        )
    )

    await db.commit()


async def synthesize_chat_tts_audio_url(
    *,
    text: str,
    voice_id: Optional[str],
    streaming_mode: int,
    speed_factor: float,
    current_user: Any,
    db: AsyncSession,
) -> Optional[str]:
    """
    [역할]
    `/chat` 응답용 TTS 합성을 수행하고 성공 시 `audio_url`만 반환한다.

    [왜 여기서 처리하나]
    - `chat.py` 라우터가 TTSRequest 생성/내부 TTS wrapper 호출 세부사항을 직접 알지 않게 해
      오케스트레이션 본문을 더 얇게 유지한다.

    [주의]
    - `app.api.tts` import는 순환 의존/초기화 순서 리스크를 줄이기 위해 함수 내부에서 lazy import 한다.
    - 실패/예외 정책(HTTPException/ValueError 매핑)은 `_synthesize_tts_internal`이 그대로 담당한다.
    """
    if not (text or "").strip():
        return None

    # Lazy import: chat router와 TTS router 간 import 결합을 최소화한다.
    from app.api.tts import TTSRequest, _synthesize_tts_internal

    tts_req = TTSRequest(
        text=text.strip(),
        voice_id=voice_id,
        streaming_mode=streaming_mode,
        return_binary=False,
        text_lang="ko",
        prompt_lang="ko",
        speed_factor=speed_factor,
    )
    tts_resp = await _synthesize_tts_internal(tts_req, current_user, db)
    if tts_resp.get("success") and tts_resp.get("data", {}).get("audio_url"):
        return tts_resp["data"]["audio_url"]
    return None
