import json
from typing import Any, Dict, List, Optional, Tuple
import logging
from sqlalchemy.ext.asyncio import AsyncSession
import tiktoken
from sqlalchemy import select
from app.core.config import settings
from app.core.llm import call_llm
from app.models.summary import ChatSummary
# Redis는 선택 사항 (ImportError 방지)
try:
    import redis
    redis_available = True
except ImportError:
    redis_available = False

logger = logging.getLogger(__name__)

class ContextManager:
    def __init__(self):
        self.is_redis_active = False
        self._memory_store = {} # 1차 캐시 (혹은 최후의 보루)

        # Redis 연결 시도
        if redis_available:
            redis_url = settings.redis_url_value
            try:
                self.redis = redis.from_url(redis_url, decode_responses=True)
                self.redis.ping()
                self.is_redis_active = True
                logger.info("ContextManager: Redis connected at %s", redis_url)
            except Exception as e:
                # [주의]
                # Redis는 선택사항이라 startup 실패를 막지 않는다.
                # 대신 운영자가 왜 memory/db fallback이 동작하는지 알 수 있게 debug 로그는 남긴다.
                logger.debug("ContextManager Redis unavailable, fallback mode enabled: %s", e)

    def _summary_key(self, session_id: str) -> str:
        """
        [역할]
        요약 캐시 저장소(Redis/Memory)에서 공통으로 쓰는 키 문자열을 생성한다.

        [왜 여기서 처리하나]
        - `summary:{session_id}` 포맷 문자열이 여러 메서드에 흩어져 있으면 오타/정책 변경 시 수정 지점이 늘어난다.
        - 저장소별 구현은 달라도 키 규칙은 단일 진실 원천으로 고정하는 편이 안전하다.

        [입력/출력]
        - 입력: 세션 ID
        - 출력: 저장소 키 문자열

        [부작용]
        - 없음

        [실패/예외]
        - 예외 없음

        [주의]
        - 키 포맷 변경은 기존 Redis 데이터/메모리 캐시 호환성에 영향이 있으므로 1차 리팩토링에서는 금지.
        """
        return f"summary:{session_id}"

    def _try_get_summary_from_redis(self, session_id: str) -> Optional[str]:
        """
        [역할]
        Redis에서 요약을 조회하고, 실패 시 `None`을 반환한다.

        [왜 여기서 처리하나]
        - Redis 조회 실패는 전체 API 실패 사유가 아니므로 공통적으로 조용히 복구해야 한다.
        - Redis 접근 예외 처리/키 생성 규칙을 한 곳에 모으면 `get_summary`가 우선순위 흐름에 집중할 수 있다.
        """
        if not self.is_redis_active:
            return None
        try:
            return self.redis.get(self._summary_key(session_id))
        except Exception:
            return None

    def _try_set_summary_to_redis(self, session_id: str, summary: str) -> None:
        """
        [역할]
        Redis 캐시에 요약을 저장하고, 실패는 무시한다.

        [왜 여기서 처리하나]
        - Redis는 캐시 계층이므로 실패를 상위로 전파하지 않는 정책을 반복 구현하지 않기 위해 분리한다.
        """
        if not self.is_redis_active:
            return
        try:
            self.redis.set(self._summary_key(session_id), summary)
        except Exception:
            return

    async def _try_get_summary_from_db(self, session_id: str, db: AsyncSession) -> Optional[str]:
        """
        [역할]
        DB에서 세션 요약을 조회하고, 실패 시 `None`을 반환한다.

        [왜 여기서 처리하나]
        - DB 조회 예외를 warning으로 남기고 fallback을 계속 타는 정책을 공통화한다.
        - `get_summary()`가 우선순위(Redis→DB→Memory)에만 집중하도록 분리한다.
        """
        try:
            stmt = select(ChatSummary).where(ChatSummary.session_id == session_id)
            result = await db.execute(stmt)
            obj = result.scalar_one_or_none()
            if obj and obj.summary:
                return obj.summary
            return None
        except Exception as e:
            logger.warning("ContextManager DB load error: %s", e)
            return None

    async def _try_save_summary_to_db(self, session_id: str, summary: str, db: AsyncSession) -> None:
        """
        [역할]
        DB에 요약을 upsert 저장하고, 실패 시 rollback 후 warning만 남긴다.

        [왜 여기서 처리하나]
        - DB 저장 로직이 `save_summary()`에 직접 있으면 Redis/Memory fallback 흐름과 섞여 읽기 어렵다.
        - 예외 처리 정책(commit/rollback/warning)을 한 곳에 고정한다.
        """
        try:
            stmt = select(ChatSummary).where(ChatSummary.session_id == session_id)
            result = await db.execute(stmt)
            obj = result.scalar_one_or_none()

            if obj:
                obj.summary = summary
            else:
                db.add(ChatSummary(session_id=session_id, summary=summary))

            await db.commit()
        except Exception as e:
            logger.warning("ContextManager DB save error: %s", e)
            await db.rollback()

    def _normalize_chat_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        """
        [역할]
        입력 메시지를 컨텍스트 관리용 표준 shape(`role`,`content`)로 정규화한다.

        [왜 여기서 처리하나]
        - `manage_context()`가 토큰 계산/슬라이딩/요약 결정까지 이미 길어서 정규화 단계까지 같이 두면 읽기 어렵다.
        - 메시지 role 정규화(`ai`→`assistant`) 규칙은 컨텍스트 관리 전역 정책이라 별도 함수가 맞다.
        """
        return [
            {
                "role": "assistant" if (m.get("role") == "ai") else m.get("role", "user"),
                "content": m.get("content", ""),
            }
            for m in messages
        ]

    def _estimate_total_context_tokens(
        self,
        *,
        enc: Any,
        messages: List[Dict[str, str]],
        persona: Optional[str],
        summary: Optional[str],
    ) -> int:
        """
        [역할]
        persona/summary/system 여유분 + 대화 메시지를 합산해 총 컨텍스트 토큰을 추정한다.

        [왜 여기서 처리하나]
        - 토큰 추정 로직은 숫자 정책(고정 오버헤드/메시지당 +10)과 강하게 결합돼 있어 재검토 포인트를 분리하는 편이 좋다.
        """
        sys_est = 600 + len(enc.encode(persona or "")) + len(enc.encode(summary or ""))
        return sys_est + sum(len(enc.encode(m.get("content", ""))) + 10 for m in messages)

    def _split_window_and_summary_candidates(
        self,
        *,
        normalized: List[Dict[str, str]],
        total_tokens: int,
        max_turns: int,
        max_context: int,
        ratio: float,
    ) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], bool]:
        """
        [역할]
        토큰/턴 기준으로 windowed 메시지와 요약 대상(to_drop)을 계산한다.

        [왜 여기서 처리하나]
        - `manage_context()`의 핵심 정책이지만 계산 로직이 길어지면 실제 I/O(save_summary) 흐름이 가려진다.
        - 정책 값(`ratio`, `max_turns`, `to_drop 40개 제한`)을 한 함수에 모아 두면 변경 시 회귀 점검이 쉽다.
        """
        if total_tokens >= max_context * ratio:
            k_eff = max(2, max_turns // 2)
        else:
            k_eff = max_turns
        k = 2 * k_eff

        if len(normalized) <= k:
            return normalized, [], False

        to_drop = normalized[:-k]
        windowed = normalized[-k:]
        if len(to_drop) > 40:
            to_drop = to_drop[-40:]
        return windowed, to_drop, True

    def _build_dialogue_text(self, messages: List[Dict[str, str]]) -> str:
        """
        [역할]
        요약 프롬프트에 넣을 대화 텍스트를 role prefix 포함 문자열로 직렬화한다.

        [왜 여기서 처리하나]
        - `summarize_dialogue()`에서 프롬프트/LLM 호출과 텍스트 직렬화가 섞이면 함수 목적이 흐려진다.
        """
        return "\n".join([f"{msg['role']}: {msg['content']}" for msg in messages])

    async def get_summary(self, session_id: str, db: Optional[AsyncSession] = None) -> str:
        """
        요약 조회 우선순위:
        1. Redis (있으면 가장 빠름)
        2. DB (영구 저장소)
        3. Memory (임시)
        """
        summary = ""
        
        # 1. Redis 조회
        summary = self._try_get_summary_from_redis(session_id)
        if summary:
            return summary

        # 2. DB 조회 (권장)
        if db:
            summary = await self._try_get_summary_from_db(session_id, db)
            if summary:
                # Redis 캐시 갱신 (Write-around 처럼)
                self._try_set_summary_to_redis(session_id, summary)
                return summary

        # 3. Memory 조회 (DB 없음 등)
        return self._memory_store.get(self._summary_key(session_id), "")

    async def save_summary(self, session_id: str, summary: str, db: Optional[AsyncSession] = None):
        """
        요약 저장:
        1. DB 저장 (영구)
        2. Redis 저장 (캐시)
        3. Memory 저장 (Fallback)
        """
        # 1. DB 저장
        if db:
            await self._try_save_summary_to_db(session_id, summary, db)

        # 2. Redis 저장
        self._try_set_summary_to_redis(session_id, summary)

        # 3. Memory 저장
        self._memory_store[self._summary_key(session_id)] = summary

    async def manage_context(
        self,
        messages: List[Dict],
        summary: str,
        session_id: str,
        db: Optional[AsyncSession],
        persona: Optional[str],
        max_turns: int,
        max_context: int,
        ratio: float,
    ) -> Tuple[List[Dict], str, bool]:
        """
        턴 기반 슬라이딩 윈도우 + tiktoken 80% 트리거.
        - messages를 {"role":"assistant"|"user", "content"}로 정규화
        - total >= max_context*ratio 이면 K_eff 축소 후 슬라이드·요약·save_summary
        - to_drop이 40개 초과 시 to_drop[-40:]만 요약
        반환: (windowed_messages, summary, did_summarize)
        """
        enc = tiktoken.get_encoding("cl100k_base")
        # 1. 정규화
        normalized = self._normalize_chat_messages(messages)
        # 2. 토큰 추정
        total = self._estimate_total_context_tokens(
            enc=enc,
            messages=normalized,
            persona=persona,
            summary=summary,
        )
        # 3. 슬라이딩 윈도우 / 요약 대상 계산
        windowed, to_drop, did_summarize = self._split_window_and_summary_candidates(
            normalized=normalized,
            total_tokens=total,
            max_turns=max_turns,
            max_context=max_context,
            ratio=ratio,
        )
        if not did_summarize:
            return (windowed, summary or "", False)
        new_summary = await self.summarize_dialogue(to_drop, previous_summary=summary or "")
        await self.save_summary(session_id, new_summary, db)
        return (windowed, new_summary, True)

    async def summarize_dialogue(self, messages: List[Dict[str, str]], previous_summary: str = "") -> str:
        """
        LLM을 사용한 요약 생성 (기존 로직 유지)
        """
        if not messages:
            return previous_summary

        dialogue_text = self._build_dialogue_text(messages)
        
        system_prompt = f"""
        당신은 드라마 보조 작가입니다.
        주어진 '이전 줄거리'와 '최근 대화'를 바탕으로, 전체 이야기를 아우르는 **새로운 줄거리 요약본**을 작성하세요.
        
        [규칙]
        1. 현재 상황과 캐릭터 간의 관계 변화를 중심으로 서술하세요.
        2. 분량은 3~5문장으로 요약하세요.
        3. '이전 줄거리'의 내용을 포함하여 흐름이 끊기지 않게 하세요.
        4. 제3자 관점(서술형)으로 작성하세요.
        5. 총 400자 이내로 압축하세요.
        
        [이전 줄거리]
        {previous_summary if previous_summary else "(없음)"}
        """

        messages_payload = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"최근 대화:\n{dialogue_text}"}
        ]

        try:
            result = await call_llm(messages_payload, temperature=0.5, max_tokens=500)
            summary = (result.get("content", "") or "") if isinstance(result, dict) else ""
            return summary.strip()
        except Exception as e:
            logger.warning("ContextManager summarization failed: %s", e)
            return previous_summary

context_manager = ContextManager()
