from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import httpx
import uuid
import logging
import json
from app.core.config import settings
from app.api.deps import get_db, get_current_user_optional, get_current_principal_optional, AuthPrincipal
from app.models.user import User
from app.models.character import Character
from app.core.llm import call_llm, call_llm_stream
from app.services.context_manager import context_manager
from app.services import chat_service, character_service, history_service
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

router = APIRouter()
logger = logging.getLogger(__name__)

_BAN_POLYSEMY = "다의어(예: 힘멜) 의미 나열·분류·설명 금지. 대사만, 모르면 '그게 뭐야?' 등."

def truncate_to_sentence(text: str, max_len: int) -> str:
    """
    문장 종결형으로 깔끔하게 자르는 유틸 함수.
    max_len 이내에서 가장 마지막 문장 종결 위치까지만 반환.
    종결 패턴: . ? ! 。및 한국어 종결 어미(다, 요, 음, 죠, 네, 나, 까, 지, 아, 야)
    """
    if not text or len(text) <= max_len:
        return text
    
    # max_len까지 자른 뒤, 마지막 종결 위치 탐색
    truncated = text[:max_len]
    
    # 종결 문자/어미 패턴 (뒤에서부터 탐색)
    ending_chars = '.?!。'
    korean_endings = ('다.', '요.', '음.', '죠.', '네.', '나.', '까.', '지.', '아.', '야.',
                      '다', '요', '음', '죠', '네', '까', '지')
    
    # 1. 마지막 마침표/물음표/느낌표 찾기
    last_punct = -1
    for i in range(len(truncated) - 1, -1, -1):
        if truncated[i] in ending_chars:
            last_punct = i
            break
    
    # 2. 한국어 종결 어미 찾기 (마침표 없이 끝나는 경우)
    if last_punct == -1:
        for ending in korean_endings:
            pos = truncated.rfind(ending)
            if pos > last_punct:
                last_punct = pos + len(ending) - 1
    
    # 종결 위치가 있으면 거기까지, 없으면 원래 max_len에서 적당히 자름
    if last_punct > max_len // 3:  # 너무 짧아지면 그냥 자르기
        return truncated[:last_punct + 1]
    else:
        # 공백에서 자르기 (단어 중간 끊김 방지)
        last_space = truncated.rfind(' ')
        if last_space > max_len // 2:
            return truncated[:last_space] + "..."
        return truncated + "..."


def format_for_first_dialogue(
    character_name: str,
    persona: str,
    opponent: str,
    situation: str,
    background: Optional[str] = None
) -> tuple:
    """
    주연 모드 첫 대사 전용: (system_prompt, user_message) 반환.
    """
    bg = f"\n- 배경: {background}" if background else ""
    system_prompt = f"""
[성격 및 설정]
- 이름: {character_name}
- 상대: {opponent}
- 상황: {situation}{bg}

{persona}

[Tone Instruction / 말투 지침 - 절대 준수]
- 반말만 사용. 존댓말·해요체·합쇼체 **절대** 금지.
- 금지 예: "~시나요", "~드시나요", "~이로군요", "이야기해주실 수 있을까요?" 등.

[절대 금지]
1. **캐릭터를 인터뷰하듯 질문 금지**: "어떤 기분이 드시나요?", "이야기해주실 수 있을까요?" 등. **당신이 그 캐릭터이므로 대사만 하세요.**
2. **나레이터/제3자 시점·해설 금지**: 지문형 해설, "그는 ...라고 생각했다" 등.
3. **세계관을 깨는 말·행동 금지**: 작품 밖 단어·개념, 4차원 대사·메타 발화.
4. 이모티콘 금지. 대사 앞에 이름 붙이지 마세요.
5. "죄송합니다", "저는 AI" 등 상담원/현실 복귀 멘트 금지. 세계관 밖 단어는 모르는 척.
6. {_BAN_POLYSEMY}

[첫 대사 지시]
아래 설정과 상황을 반영해, 이 상황에서 상대('{opponent}')에게 하는 **첫 마디 한 문장**만 생성하세요. 인사·반응·독백 등 캐릭터에 맞게 대사만 출력하세요. 배경이 주어졌다면 그 분위기와 맥락도 반영하세요.
"""
    user_message = "이 상황에서 캐릭터의 첫 마디만 한 문장으로 생성하세요."
    return (system_prompt, user_message)


def format_persona_for_actor(
    character_name: str,
    persona: str,
    partner_name: str,
    situation: str,
    turn_count: int = 0,
    director_note: Optional[str] = None,
    summary: Optional[str] = None,
    background: Optional[str] = None,
    is_director_mode: bool = False
) -> str:
    """
    4k 컨텍스트 최적화: 압축된 시스템 프롬프트 (~1.2k 토큰 목표)
    핵심 페르소나 보존, 중복 제거, 짧은 규칙 사용
    """
    # 기본 규칙 (압축)
    system_prompt = f"""[캐릭터] 당신={character_name}. AI 아님. 이 삶이 현실.
[상대] {partner_name} [상황] {situation}
{persona}

[말투] 반말만. 존댓말/해요체 절대 금지. (안녕하세요→안녕, 그랬어요?→그랬어?)

[금지]
• 사과/상담원 멘트 ("죄송","몰입했나봐요") → 캐릭터로 맞받아치기
• AI 정체 노출 ("저는 AI","현실로") → 세계관 속 인물로 반응
• 무조건 동조 금지. 멍청한 소리엔 화/무시. 성격대로 반응
• 세계관 밖 단어(스마트폰,PC방 등) → "그게 뭐야?" 모르는 척
• 이모지 금지. 이름 접두사 금지. 인터뷰/해설/다의어 설명 금지"""

    if is_director_mode:
        system_prompt += """
• [감독모드] 감독에게 말걸기/언급 금지. 극중 인물만 연기"""

    # 상황 정보 추가 (간결하게)
    if background:
        system_prompt += f"\n[배경] {background}"
    if summary:
        system_prompt += f"\n[이전] {summary}"
    if director_note:
        system_prompt += f"\n[감독지시] {director_note}"
    if turn_count >= 9:
        system_prompt += "\n(대화 마무리 시간. 여운 남기며 종결.)"

    system_prompt += f"\n\n→ 오직 {character_name}의 입으로만 대답."

    return system_prompt


async def get_character_by_id(
    character_id: str,
    user_id: str,
    db: AsyncSession
) -> Optional[Character]:
    """캐릭터 정보 조회 (사전설정 또는 사용자 소유)"""
    try:
        result = await db.execute(
            select(Character).where(Character.id == character_id)
        )
        character = result.scalar_one_or_none()
        
        if character:
            if character.is_preset or character.user_id == user_id:
                return character
        
        return None
    except Exception as e:
        logger.warning(f"캐릭터 조회 실패: {e}")
        return None


class Message(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: List[Message]
    persona: Optional[str] = None
    temperature: float = 0.8
    max_tokens: int = 1024  # 응답 길이 여유 확보
    model: str = "gemma-3-27b"
    session_id: Optional[str] = None
    character_id: Optional[str] = None
    scenario: Optional[Dict[str, str]] = None
    director_note: Optional[str] = None  # 감독의 긴급 지시
    current_speaker: Optional[str] = None  # 현재 말할 캐릭터 (감독 모드용)
    # TTS 관련 필드
    tts_enabled: bool = True
    tts_mode: str = "realtime"
    tts_delay_ms: int = 0
    tts_streaming_mode: int = 0
    tts_speed: float = 1.0  # 발화 속도 (1.0=정속, 0.5~2.0)


def _resolve_speaker_name_for_response(
    *,
    speaker_name: Optional[str],
    resolved_character_name: Optional[str],
    opponent: str,
    is_director_mode: bool,
    current_speaker: Optional[str],
) -> str:
    """
    [역할]
    `/chat` 응답 후처리/저장/TTS에 사용할 최종 화자명을 일관된 규칙으로 결정한다.

    [왜 여기서 처리하나]
    - `/chat` 본문에서 감독모드/일반모드 분기 후 `speaker_name` 재조합 규칙이 흩어지면,
      저장명/응답명/TTS 화자명이 미세하게 달라지는 회귀가 생기기 쉽다.

    [주의]
    - 감독 모드에서는 `current_speaker`가 최우선이다. (프론트가 기대하는 현재 발화자 표시와 맞춰야 함)
    """
    final_name = speaker_name or resolved_character_name or opponent
    if is_director_mode and current_speaker:
        final_name = current_speaker
    return final_name


async def _run_chat_turn_llm_and_persist(
    *,
    db: AsyncSession,
    session_id: str,
    request: ChatRequest,
    chat_messages: List[Dict[str, str]],
    system_instruction: Optional[str],
    speaker_name: str,
) -> tuple[str, Dict[str, Any], Dict[str, Optional[str]]]:
    """
    [역할]
    `/chat`의 LLM 호출 -> 응답 후처리 -> DB 저장/commit 흐름을 한 번에 수행한다.

    [왜 여기서 처리하나]
    - 라우터에서 LLM 호출/정제/저장을 모두 직접 처리하면 예외 매핑과 응답 조립 로직이 섞여 읽기 어렵다.
    - 이 helper는 "한 턴 처리" 단위를 고정해 `chat()`가 transport + 응답 조립에 집중하게 만든다.

    [부작용]
    - 외부 LLM 호출
    - DB insert/commit (사용자 메시지/assistant 메시지 저장)

    [실패/예외]
    - `call_llm`/DB 저장 예외는 상위 라우터로 올린다. (라우터가 mock fallback/HTTPException 정책 담당)

    [주의]
    - `sanitize_assistant_reply_content`를 통하지 않으면 speaker prefix/형식 후처리 규칙이 깨질 수 있다.
    """
    result = await call_llm(
        messages=chat_messages,
        model=request.model,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        system_instruction=system_instruction,
    )
    content = chat_service.sanitize_assistant_reply_content(result["content"], speaker_name)
    persisted = await chat_service.persist_chat_turn_messages(
        db=db,
        session_id=session_id,
        raw_request_messages=request.messages,
        assistant_content=content,
        speaker_name=speaker_name,
    )
    return content, result["usage"], persisted


async def _maybe_attach_tts_audio_url(
    *,
    response_data: Dict[str, Any],
    content: str,
    request: ChatRequest,
    current_user: Optional[User],
    db: AsyncSession,
) -> Optional[Dict[str, Any]]:
    """
    [역할]
    `/chat` 응답에 TTS `audio_url`을 조건부로 추가한다.

    [왜 여기서 처리하나]
    - TTS는 `/chat` 성공 응답의 "부가 기능"이므로, 본문 LLM/DB 저장 성공 흐름과 실패 격리가 필요하다.
    - 별도 helper로 분리하면 `audio_url` 누락 시에도 기본 채팅 응답 계약을 안정적으로 유지할 수 있다.

    [부작용]
    - 캐릭터 voice_id 조회(DB/preset 가능)
    - 내부 TTS 라우터 helper 호출(파일/외부 HTTP/DB 캐시 경유 가능)

    [실패/예외]
    - 여기서는 broad except로 흡수하고 경고 로그만 남긴다.
      `/chat`의 핵심 계약은 텍스트 응답 성공이며, TTS 실패가 전체 요청 실패로 전파되면 UX 회귀가 크다.

    [주의]
    - `response_data["audio_url"]` 추가는 성공 시에만 수행한다. 키를 억지로 넣으면 프론트 분기 로직이 흔들릴 수 있다.
    """
    if not request.tts_enabled or not (content or "").strip():
        return None

    voice_id = await character_service.resolve_character_voice_id_for_tts(
        character_id=request.character_id,
        db=db,
        current_user_id=current_user.id if current_user else None,
        default_voice_id="default",
    )
    try:
        tts_result = await chat_service.synthesize_chat_tts_result(
            text=content,
            voice_id=voice_id,
            streaming_mode=request.tts_streaming_mode,
            speed_factor=request.tts_speed or 1.0,
            current_user=current_user,
            db=db,
        )
        if tts_result and tts_result.get("audio_url"):
            response_data["audio_url"] = tts_result["audio_url"]
            return tts_result
    except Exception as e:
        logger.warning("TTS 합성 실패(audio_url 미포함): %s", e)
    return None


async def _generate_chat_stream_sse_events(
    *,
    chat_messages: List[Dict[str, str]],
    request: ChatRequest,
    system_instruction: Optional[str],
    session_id: str,
):
    """
    [역할]
    `/chat/stream`의 SSE 이벤트 프레임을 생성한다.

    [왜 여기서 처리하나]
    - SSE frame 포맷(`content`/`done`/`full_content`/`session_id`)은 `/chat/stream`의 핵심 외부 계약이라
      라우터 본문에서 흩어지지 않게 generator helper로 고정한다.

    [실패/예외]
    - 스트리밍 중 예외는 에러 프레임으로 변환해 연결을 정상 종료한다.
      (HTTP 예외로 바꾸면 이미 열린 SSE 연결에서 프론트가 처리하기 어려움)

    [주의]
    - `done=True` 최종 프레임 shape는 프론트 소비 코드가 강하게 의존하므로 키명/타입 변경 금지.
    """
    full_content = ""
    try:
        async for chunk in call_llm_stream(
            messages=chat_messages,
            model=request.model,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            system_instruction=system_instruction,
        ):
            full_content += chunk
            yield f"data: {json.dumps({'content': chunk, 'done': False})}\n\n"

        yield (
            "data: "
            + json.dumps(
                {
                    "content": "",
                    "done": True,
                    "full_content": full_content,
                    "session_id": session_id,
                }
            )
            + "\n\n"
        )
        logger.info("스트리밍 완료: session=%s, length=%s", session_id, len(full_content))
    except Exception as e:
        logger.error("스트리밍 오류: %s", e)
        yield f"data: {json.dumps({'error': str(e), 'done': True})}\n\n"


async def _prepare_chat_request_for_llm(
    *,
    request: ChatRequest,
    db: AsyncSession,
    session_id: str,
) -> Dict[str, Any]:
    """
    [역할]
    `/chat` 요청을 LLM 호출 직전 상태로 정규화/구성한다.

    [왜 여기서 처리하나]
    - `/chat` 본문에서 가장 복잡한 단계(시나리오 해석, 컨텍스트 관리, 감독/일반 모드 분기, 시스템 프롬프트 구성)를
      한 곳으로 모아 라우터를 얇게 유지한다.
    - 반환 dict 키는 이후 `/chat` 오케스트레이션에서 필요한 최소 상태를 명시적으로 고정한다.

    [부작용]
    - `context_manager` 호출(요약 조회/갱신, DB/Redis 접근 가능)
    - 캐릭터 정체성 조회(DB/preset 가능)

    [실패/예외]
    - 감독 모드 파싱 실패는 내부에서 로그 후 일반 모드로 폴백한다. (요청 전체 실패보다 호환성 우선)
    - 그 외 예외는 상위 라우터로 전파해 `/chat`의 최종 예외 정책으로 처리한다.

    [주의]
    - 반환 키(`opponent`, `did_summarize`, `system_instruction`, `chat_messages` 등)는
      `chat()` 후속 단계가 직접 참조하므로 이름 변경 금지.
    """
    summary = await context_manager.get_summary(session_id, db)

    scenario_fields = chat_service.extract_scenario_fields(
        request.scenario,
        default_user_name="감독",
    )
    opponent = scenario_fields["opponent"] or "상대방"
    situation = scenario_fields["situation"] or "대화 중"
    user_name = scenario_fields["user_name"] or "감독"
    background = scenario_fields["background"]

    turn_count = len(request.messages) // 2
    is_director_mode = bool(request.persona and "[배우 1:" in request.persona and "[배우 2:" in request.persona)

    request_messages = chat_service.normalize_request_messages_for_context(request.messages)
    request_messages, summary, did_summarize = await context_manager.manage_context(
        request_messages,
        summary,
        session_id,
        db,
        request.persona,
        getattr(settings, "CONTEXT_WINDOW_TURNS", 6),
        getattr(settings, "CONTEXT_MAX_TOKENS", 8192),
        getattr(settings, "CONTEXT_TOKEN_THRESHOLD_RATIO", 0.8),
    )
    summary = truncate_to_sentence(summary or "", 250)

    resolved_character_name: Optional[str] = None
    current_speaker: Optional[str] = None
    speaker_name: Optional[str] = None
    messages: List[Dict[str, str]] = []

    if is_director_mode:
        try:
            director_ctx = chat_service.parse_director_turn_context(
                persona=request.persona or "",
                messages=request.messages,
                current_speaker_override=request.current_speaker,
            )
            current_speaker = director_ctx["current_speaker"]
            speaker_persona = director_ctx["speaker_persona"]
            partner_name = director_ctx["partner_name"]
            system_prompt = format_persona_for_actor(
                character_name=current_speaker,
                persona=speaker_persona,
                partner_name=partner_name,
                situation=situation,
                turn_count=turn_count,
                director_note=request.director_note,
                summary=summary,
                background=background,
                is_director_mode=True,
            )
            messages.append({"role": "system", "content": system_prompt})
        except Exception as e:
            logger.error("감독 모드 파싱 실패: %s", e)
            is_director_mode = False

    if not is_director_mode:
        resolved_character_name = opponent or "캐릭터"
        if request.character_id:
            resolved_character_name, persona_override = await chat_service.resolve_character_identity(
                character_id=request.character_id,
                fallback_name=resolved_character_name,
                db=db,
                preset_loader=character_service.load_preset_characters_from_disk,
            )
            if persona_override:
                request.persona = persona_override

        if request.persona and request.messages:
            system_prompt = format_persona_for_actor(
                character_name=resolved_character_name,
                persona=request.persona,
                partner_name=user_name,
                situation=situation,
                turn_count=turn_count,
                director_note=request.director_note,
                summary=summary,
                background=background,
            )
            messages.append({"role": "system", "content": system_prompt})
        speaker_name = resolved_character_name

    if is_director_mode:
        speaker_name = None

    if not request_messages and not is_director_mode:
        system_fd, user_fd = format_for_first_dialogue(
            resolved_character_name or "캐릭터",
            request.persona or "",
            opponent,
            situation,
            background,
        )
        messages.append({"role": "system", "content": system_fd})
        messages.append({"role": "user", "content": user_fd})
        speaker_name = resolved_character_name or "캐릭터"
    else:
        chat_service.append_history_messages(
            messages_acc=messages,
            request_messages=request_messages,
            default_user_message="대화를 시작해주세요.",
        )

    system_instruction, chat_messages = chat_service.split_system_and_chat_messages(messages)
    return {
        "opponent": opponent,
        "did_summarize": did_summarize,
        "resolved_character_name": resolved_character_name,
        "is_director_mode": is_director_mode,
        "current_speaker": current_speaker,
        "speaker_name": speaker_name,
        "system_instruction": system_instruction,
        "chat_messages": chat_messages,
    }

@router.post("/chat")
async def chat(
    request: ChatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
    principal: Optional[AuthPrincipal] = Depends(get_current_principal_optional),
):
    """
    배우 모드: AI가 한 캐릭터로서 한 턴만 응답
    감독 모드: 두 캐릭터가 교대로 응답

    [왜 여기서 이렇게 나누나]
    - 준비 단계(`_prepare_chat_request_for_llm`)와 실행 단계(`_run_chat_turn_llm_and_persist`)를 분리해,
      라우터는 예외 매핑/응답 shape/TTS 부가 기능 처리 중심으로 유지한다.

    [주의]
    - broad except의 mock fallback 응답은 현재 프론트/개발 흐름 호환을 위한 계약 일부다.
      제거/축소는 2차 정책에서 명시적으로 결정해야 한다.
    """
    session_id = request.session_id or str(uuid.uuid4())
    if principal and principal.kind == "guest" and principal.guest_session:
        logger.info("Guest chat request session_id=%s guest_session_id=%s", session_id, principal.guest_session.id)
    try:
        # history root 선점: 동일 session_id 재사용으로 타인 기록에 append하는 시도를 초기에 차단한다.
        await history_service.ensure_conversation_for_session(
            db=db,
            session_id=session_id,
            principal=principal,
            request=request,
            create_if_missing=False,
        )
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except Exception as e:
        logger.warning("History ownership precheck skipped (non-fatal): %s", e)

    prepared = await _prepare_chat_request_for_llm(
        request=request,
        db=db,
        session_id=session_id,
    )
    opponent = prepared["opponent"]
    did_summarize = prepared["did_summarize"]
    resolved_character_name = prepared["resolved_character_name"]
    is_director_mode = prepared["is_director_mode"]
    current_speaker = prepared["current_speaker"]
    speaker_name = prepared["speaker_name"]
    system_instruction = prepared["system_instruction"]
    chat_messages = prepared["chat_messages"]

    try:
        speaker_name = _resolve_speaker_name_for_response(
            speaker_name=speaker_name,
            resolved_character_name=resolved_character_name,
            opponent=opponent,
            is_director_mode=is_director_mode,
            current_speaker=current_speaker,
        )
        content, usage, persisted_meta = await _run_chat_turn_llm_and_persist(
            db=db,
            session_id=session_id,
            request=request,
            chat_messages=chat_messages,
            system_instruction=system_instruction,
            speaker_name=speaker_name,
        )

        conversation = None
        try:
            conversation = await history_service.update_conversation_after_chat_turn(
                db=db,
                session_id=session_id,
                principal=principal,
                request=request,
                assistant_content=content,
                speaker_name=speaker_name,
            )
        except PermissionError as e:
            logger.warning("History conversation update denied (non-fatal): %s", e)
        except Exception as e:
            logger.warning("History conversation update failed (non-fatal): %s", e)

        response_data = {
            "content": content,
            "usage": usage,
            "session_id": session_id,
            "context_summarized": did_summarize
        }

        tts_result = await _maybe_attach_tts_audio_url(
            response_data=response_data,
            content=content,
            request=request,
            current_user=current_user,
            db=db,
        )
        if conversation and tts_result:
            try:
                await history_service.attach_conversation_audio_asset_from_chat_tts(
                    db=db,
                    principal=principal,
                    conversation_id=conversation.id,
                    message_id=persisted_meta.get("assistant_message_id"),
                    tts_data=tts_result,
                    fallback_voice_id=tts_result.get("voice_id"),
                )
            except Exception as e:
                logger.warning("History audio asset attach failed (non-fatal): %s", e)

        return {
            "success": True,
            "data": response_data
        }

    except httpx.HTTPStatusError as e:
        logger.error(f"LLM service error: {e.response.status_code}")
        raise HTTPException(status_code=e.response.status_code, detail=f"LLM 서비스 에러: {str(e)}")
    except Exception as e:
        logger.error(f"Chat API error: {e}")
        # 연결 실패 시 Mock 응답 (사용자 요청 반영)
        return {
            "success": True,
            "data": {
                "content": "[테스트 모드] 현재 AI 서버 연결이 원활하지 않아 준비된 대사를 출력합니다. 상황 설정을 확인해 주세요!",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                "session_id": session_id,
                "context_summarized": False
            }
        }


@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
    principal: Optional[AuthPrincipal] = Depends(get_current_principal_optional),
):
    """
    SSE 스트리밍 채팅 엔드포인트.
    Gemini 응답을 실시간으로 청크 단위로 전송하여 체감 응답 속도 향상.
    Content-Type: text/event-stream

    [왜 `/chat`와 로직이 일부 분리돼 있나]
    - `/chat/stream`은 SSE 연결/프레임 생성 계약이 핵심이라 `/chat`의 DB 저장/TTS 후처리 흐름과 우선순위가 다르다.
    - 1차 리팩토링에서는 응답 필드/SSE 동작 호환성이 더 중요해서 일부 구성 로직만 재사용한다.

    [주의]
    - 빈 대화/기본 프롬프트 동작은 `/chat`와 일부 다르게 유지될 수 있다(기존 동작 호환).
    """
    session_id = request.session_id or str(uuid.uuid4())
    if principal and principal.kind == "guest" and principal.guest_session:
        logger.info("Guest chat stream request session_id=%s guest_session_id=%s", session_id, principal.guest_session.id)
    
    # 시나리오 정보 추출 (기존 /chat 로직과 동일)
    scenario_fields = chat_service.extract_scenario_fields(
        request.scenario,
        default_user_name="사용자",
    )
    opponent = scenario_fields["opponent"] or "상대방"
    situation = scenario_fields["situation"] or "대화 중"
    user_name = scenario_fields["user_name"] or "사용자"
    background = scenario_fields["background"]
    
    # 턴 카운트
    turn_count = len(request.messages) // 2
    
    # 메시지 구성
    messages = []
    
    # 시스템 프롬프트 생성 (간소화 버전)
    if request.persona:
        # resolved_character_name 추출 (Preset → DB 순서 정책은 service에 위임)
        resolved_character_name, _ = await chat_service.resolve_character_identity(
            character_id=request.character_id,
            fallback_name=opponent,
            db=db,
            preset_loader=character_service.load_preset_characters_from_disk,
        )

        system_prompt = format_persona_for_actor(
            character_name=resolved_character_name,
            persona=request.persona,
            partner_name=user_name,
            situation=situation,
            turn_count=turn_count,
            director_note=request.director_note,
            summary=None,
            background=background
        )
        messages.append({"role": "system", "content": system_prompt})
    
    # 대화 기록 추가 (정규화 규칙만 재사용; `/chat/stream`의 빈 대화 동작은 기존대로 유지)
    stream_request_messages = chat_service.normalize_request_messages_for_context(request.messages)
    for msg in stream_request_messages:
        messages.append({"role": msg["role"], "content": msg["content"]})
    
    # 시스템 프롬프트 분리 규칙은 `/chat`과 동일하게 유지
    system_instruction, chat_messages = chat_service.split_system_and_chat_messages(messages)

    return StreamingResponse(
        _generate_chat_stream_sse_events(
            chat_messages=chat_messages,
            request=request,
            system_instruction=system_instruction,
            session_id=session_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Nginx 버퍼링 비활성화
        }
    )
