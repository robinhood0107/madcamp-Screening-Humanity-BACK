from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
import logging
from app.api.deps import get_current_user
from app.models.user import User
from app.services import character_service, story_service

router = APIRouter()
logger = logging.getLogger(__name__)

def _extract_json(content: str) -> Dict[str, Any]:
    """LLM 응답에서 JSON 객체를 추출합니다."""
    # [왜 wrapper로 남기나]
    # 기존 문서/호출 흐름에서 `_extract_json` 이름이 이미 기준점 역할을 하므로
    # 1차는 함수명을 유지하고 실제 구현만 공통 서비스로 위임한다.
    return character_service.extract_json_object_from_llm_text(content)

# ============ 스토리 생성 API ============
class StoryGenerationRequest(BaseModel):
    """스토리 생성 요청 모델"""
    situation: str = Field(..., description="사용자가 입력한 짧은 상황")
    opponent_name: str = Field(None, description="상대방 캐릭터 이름")
    character_persona: str = Field(None, description="캐릭터 페르소나 설정")
    model: Optional[str] = Field(None, description="사용할 LLM 모델 (gemini-2.5-flash 또는 gemma-3-27b)")

class StoryGenerationResponse(BaseModel):
    """스토리 생성 응답 모델"""
    plot: str

@router.post("/generate/story")
async def generate_story(
    request: StoryGenerationRequest,
):
    """
    사용자의 상황 입력을 바탕으로 드라마틱한 줄거리 생성
    """
    
    # ... (이전 코드 생략) ...
    user_n = "나"
    char_n = request.opponent_name or "상대방"

    system_prompt = story_service.build_story_generation_system_prompt(
        user_name=user_n,
        opponent_name=char_n,
    )
    messages = story_service.build_story_generation_messages(
        system_prompt=system_prompt,
        situation=request.situation,
    )
    
    try:
        content = await story_service.call_story_llm_with_selection(
            messages=messages,
            user_selected_model=request.model,
            temperature=0.8,
            max_tokens=4000,
            logger_hint="Story generation (/api/generate/story)",
        )
        plot, background = story_service.parse_story_sections(content)

        return {
            "success": True,
            "data": {
                "plot": plot,
                "background": background
            }
        }
    except Exception as e:
        # broad except 유지 이유:
        # 이 엔드포인트는 "LLM 장애 시 mock 스토리로 계속 진행"이 UX 계약의 일부라서
        # 외부 LLM/파싱/포맷 오류를 한 곳에서 흡수해 success shape를 유지한다.
        logger.error("Story generation failed, using mock: %s", e)
        mock_story = story_service.build_story_mock_plot(request.situation)
        return {
            "success": True,
            "data": {
                "plot": mock_story,
                "background": ""
            }
        }

@router.post("/story/analyze")
async def analyze_story_legacy(request: StoryGenerationRequest):
    """구형 엔드포인트 호환용 (/api/story/analyze)"""
    return await generate_story(request)

class CharacterGenerationRequest(BaseModel):
    """캐릭터 생성 요청 모델 - 상세 필드 지원"""
    name: str = Field(..., description="캐릭터 이름")
    category: Optional[str] = Field(None, description="카테고리")
    source_work: Optional[str] = Field(None, description="작품명 (출처)")
    description: str = Field("", description="캐릭터 컨셉/설명")
    worldview: Optional[str] = Field(None, description="세계관")

class CharacterGenerationResponse(BaseModel):
    """캐릭터 생성 응답 모델"""
    success: bool
    data: Dict[str, Any]

@router.post("/generate/character-details")
async def generate_character_details(
    request: CharacterGenerationRequest,
    current_user: User = Depends(get_current_user)
):
    """
    이름과 기본 설명을 바탕으로 캐릭터의 상세 설정(페르소나, 성격, 말투, 세계관 등 16종)을 자동 생성
    """

    # [왜 이 라우터에서 처리하나]
    # - `/generate/character-details`는 프론트가 직접 호출하는 공개 계약이라 path/응답 shape를 라우터에서 고정해야 한다.
    # - 실제 LLM fallback 호출/JSON 추출/리스트 필드 보정은 service helper를 쓰되,
    #   "어떤 입력을 어떤 프롬프트 규칙으로 생성하는지" 오케스트레이션 순서는 여기서 읽히게 유지한다.
    #
    # [주의]
    # - `current_user`는 현재 본문에서 직접 사용하지 않지만, 인증이 필요한 생성 엔드포인트라는 정책을 dependency로 고정한다.
    # - 응답의 `success`, `is_fallback`, `data` key는 프론트 계약이라 1차 리팩토링에서 변경 금지다.

    # 1) 시스템 프롬프트 구성
    # [역할]
    # - 출력 언어/형식(JSON only)/필수 필드/길이 제약(worldview 300자+)을 여기서 고정한다.
    # - provider가 바뀌어도 "요구 스키마" 계약은 이 문자열이 기준이 된다.
    system_prompt = (
        "당신은 창의적이고 엄격한 캐릭터 설정 전문가입니다. "
        "사용자가 제공한 정보를 바탕으로 캐릭터의 상세 설정을 '한국어'로 작성하여 JSON 형식으로 응답해주세요. "
        "특히 '작품명'이 제공되면 해당 작품의 고증을 철저히 지켜 정보를 채워주세요.\n\n"
        "## 요구사항\n"
        "1. **세계관(worldview)**은 반드시 **300자 이상**으로 풍부하게 작성해야 합니다. (시대, 장소, 물리/마법 법칙, 금기, 분위기 등 포함)\n"
        "2. 모든 필드를 빠짐없이 채워주세요.\n"
        "3. 응답은 오직 JSON 형식이어야 합니다.\n\n"
        "## JSON 스키마\n"
        "{\n"
        "  \"name\": \"캐릭터 이름\",\n"
        "  \"gender\": \"성별\",\n"
        "  \"species\": \"종족\",\n"
        "  \"age\": \"나이\",\n"
        "  \"height\": \"키\",\n"
        "  \"job\": \"직업\",\n"
        "  \"worldview\": \"세계관 (최소 300자 필수)\",\n"
        "  \"personality\": \"성격 (콤마로 구분된 특성들)\",\n"
        "  \"appearance\": \"외모 (머리, 눈, 체형, 복장 등 상세 묘사)\",\n"
        "  \"description\": \"설명 (배경 스토리, 현재 상황)\",\n"
        "  \"likes\": [\"좋아하는 것1\", \"좋아하는 것2\"], \n"
        "  \"dislikes\": [\"싫어하는 것1\", \"싫어하는 것2\"], \n"
        "  \"speech_style\": \"말투 (어조, 어미 특징)\",\n"
        "  \"thoughts\": \"생각 (속마음 대사 예시)\",\n"
        "  \"features\": \"특징 (행동 패턴, 습관)\",\n"
        "  \"habits\": \"말버릇\",\n"
        "  \"guidelines\": \"가이드라인 (롤플레이 주의사항)\"\n"
        "}"
    )
    
    # 2) 사용자 입력 정규화
    # [주의]
    # - None 허용 필드는 사람이 읽기 쉬운 기본값으로 치환해서 프롬프트 누락을 줄인다.
    # - request 원본 shape는 건드리지 않고, LLM 입력 문자열만 조립한다.
    user_input_parts = [
        f"이름: {request.name}",
        f"카테고리: {request.category or '미지정'}",
        f"작품명(출처): {request.source_work or '오리지널'}",
        f"기본설명: {request.description}"
    ]
    if request.worldview:
        user_input_parts.append(f"추가 세계관 설정: {request.worldview}")
        
    user_input = "\n".join(user_input_parts)
    
    # 3) LLM 메시지 구성
    # - system prompt는 `call_llm_json_with_gemini_fallback()`에 별도 인자로 넘기고,
    #   user 메시지에는 실제 캐릭터 컨텍스트만 담는다.
    messages = [
        {"role": "user", "content": user_input}
    ]
    
    # 4) LLM 호출 (Gemini -> Ollama fallback)
    # [실패 정책]
    # 1차 리팩토링에서는 Gemini/Ollama fallback 체인이 모두 실패해도 500 응답 shape를 유지한다.
    # (세부 provider 에러는 로그로 남기고, 라우터 detail 접두사는 고정)
    try:
        content, is_fallback = await character_service.call_llm_json_with_gemini_fallback(
            messages=messages,
            system_instruction=system_prompt,
            fallback_model="gemma-3-27b",
            temperature=0.7,
            max_tokens=2000,
            logger_hint="Character generation (/api/generate/character-details)",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Character generation LLM fallback chain failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Character generation failed (Gemini & Ollama): {str(e)}"
        )

    # 5) JSON 파싱 + 후처리
    # - `_extract_json`은 fenced code block/여분 텍스트가 섞인 응답에서 JSON 객체를 추출한다.
    # - `normalize_string_list_fields`는 likes/dislikes가 문자열로 오는 경우 배열로 보정한다.
    # - 1차 리팩토링에서는 "필드 존재 여부/길이/스키마 엄격 검증"까지 라우터에서 강제하지 않는다
    #   (기존 생성 UX/호환성 유지 우선). 2차에서 스키마 검증 계층 추가 후보.
    try:
        details = _extract_json(content)
        character_service.normalize_string_list_fields(details, ["likes", "dislikes"])
    except ValueError as e:
        # JSON 추출 실패는 LLM 출력 형식 문제이지만, 외부 계약상 500으로 유지한다.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to parse generation result: {str(e)}"
        )
    except Exception as e:
        # broad except 유지 이유: 파싱 후 후처리(normalize)에서 발생 가능한 비정형 타입 오류도 500 shape로 고정
        logger.exception("Character generation parse/post-process failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to parse generation result: {str(e)}"
        )
    
    return {
        "success": True,
        "is_fallback": is_fallback,
        "data": details
    }
            

