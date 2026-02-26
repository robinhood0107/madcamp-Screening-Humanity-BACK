from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.deps import get_db
from app.core.llm import call_llm
from app.services.context_manager import context_manager
from app.services import evaluation_data_gateway
import json
import logging

router = APIRouter()
logger = logging.getLogger(__name__)

class Message(BaseModel):
    role: str
    content: str

class EvaluationRequest(BaseModel):
    messages: List[Message]
    character_name: str
    character_persona: str
    user_name: Optional[str] = "사용자"
    session_id: Optional[str] = "default_session"

@router.post("/evaluate")
async def evaluate_chat(
    request: EvaluationRequest,
    db: AsyncSession = Depends(get_db)  # DB 세션 주입
):
    """
    [역할]
    대화 기록을 캐릭터 시점으로 평가하고 요약/점수/피드백을 생성한 뒤 컨텍스트 요약을 저장한다.

    [왜 여기서 처리하나]
    이 엔드포인트는 "평가 LLM 호출 + 요약 영속화(context_manager)"를 한 번에 수행하는 오케스트레이션 경계다.
    1차 리팩토링에서는 응답 shape를 유지하면서 단계 주석으로 흐름만 명확히 한다.

    [주의]
    평가/요약 실패 시에도 `success=True` fallback 응답을 반환하는 현재 UX 계약을 유지한다.
    """
    
    # 1) 캐릭터 빙의 평가 프롬프트 구성
    system_prompt = f"""
    당신은 '{request.character_name}'입니다. 다음 페르소나를 완벽하게 연기하세요.
    
    [페르소나]
    {request.character_persona}

    [임무]
    지금까지 당신과 대화한 파트너('{request.user_name}')를 평가해야 합니다.
    제3자가 아닌, **반드시 '{request.character_name}'의 입장에서** 말하세요.
    
    [입력 대화]
    사용자와 당신(AI)이 나눈 대화 기록이 제공됩니다.

    [작성 항목 (JSON 형식)]
    1. summary (대화 요약): 3줄 내외로 우리 사이에 무슨 일이 있었는지 요약. (서술형)
    2. score (호감도/몰입도): 0~100점. 당신이 느끼기에 상대방이 얼마나 매력적이거나 대화가 즐거웠는지.
    3. feedback (한줄 평): 당신의 말투로 상대방에게 건네는 코멘트.

    [중요] 오직 JSON 포맷으로만 응답하세요. 마크다운 코드 블록(```json)이나 다른 설명(사족)을 붙이지 마세요. 순수한 JSON 문자열만 출력하세요.
    
    [JSON 예시]
    {{
        "summary": "처음에는 어색했지만 취미 이야기를 하며 가까워졌다. 특히 영화 취향이 잘 맞았다.",
        "score": 85,
        "feedback": "너 꽤 재밌는 녀석이네? 다음에 또 얘기하자."
    }}
    """

    # 2) 대화 기록 직렬화 (LLM 입력용)
    dialogue_text = "\n".join([f"{msg.role}: {msg.content}" for msg in request.messages])

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"대화 기록:\n{dialogue_text}"}
    ]

    try:
        # 3) 평가 LLM 호출 + JSON 파싱
        result = await call_llm(messages, temperature=0.7, max_tokens=800)
        
        content = result if isinstance(result, str) else result.get("content", "")
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
            
        data = json.loads(content)
        
        # 4) DB/Redis 요약 갱신 (context_manager가 내부 저장소 fallback 정책을 캡슐화)
        # 이전 요약 가져오기 (DB에서)
        prev_summary = await context_manager.get_summary(request.session_id, db)
        
        # 새로운 통합 요약 생성
        final_summary = await context_manager.summarize_dialogue(
            [m.dict() for m in request.messages], 
            previous_summary=prev_summary
        )
        
        # DB에 저장
        await context_manager.save_summary(request.session_id, final_summary, db)
        
        data["summary"] = final_summary
        
        response_payload = {
            "score": data.get("score"),
            "feedback": data.get("feedback") or "",
            "summary": final_summary,
        }

        # 5) 평가 결과 영속화 (data-service rehearsal gateway -> SQLAlchemy/text fallback)
        try:
            persist_result = await evaluation_data_gateway.create_evaluation_for_legacy_session(
                db=db,
                legacy_session_id=str(request.session_id or "default_session"),
                score=response_payload["score"] if isinstance(response_payload["score"], int) else None,
                summary=str(response_payload["summary"] or ""),
                feedback=str(response_payload["feedback"] or ""),
                raw_payload=data if isinstance(data, dict) else None,
            )
            if not persist_result.get("created"):
                logger.info(
                    "Evaluation persistence skipped/failed session_id=%s reason=%s",
                    request.session_id,
                    persist_result.get("reason"),
                )
        except Exception:
            logger.exception("Evaluation persistence gateway failed session_id=%s", request.session_id)

        return {
            "success": True,
            "data": data
        }

    except Exception as e:
        # broad except 유지 이유:
        # LLM 호출/JSON 파싱/요약 저장 중 어디서 실패하든 현재 UX 계약은 "fallback 평가 결과 반환"이기 때문이다.
        logger.warning("Evaluation fallback used: %s", e)
        fallback_data = {
            "score": 50,
            "feedback": "대화 데이터를 분석할 수 없었어요...",
            "summary": "알 수 없는 이유로 대화가 중단되었습니다."
        }

        # fallback 평가도 best-effort로 저장 시도 (기록 상세에서 평가 탭 재사용 대비)
        try:
            await evaluation_data_gateway.create_evaluation_for_legacy_session(
                db=db,
                legacy_session_id=str(request.session_id or "default_session"),
                score=int(fallback_data["score"]),
                summary=str(fallback_data["summary"]),
                feedback=str(fallback_data["feedback"]),
                raw_payload={
                    "fallback": True,
                    "reason": str(e),
                },
            )
        except Exception:
            logger.exception("Fallback evaluation persistence failed session_id=%s", request.session_id)

        return {
            "success": True,
            "data": fallback_data
        }
