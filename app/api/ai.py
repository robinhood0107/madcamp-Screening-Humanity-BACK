from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from app.core.llm import call_llm
from app.api.deps import get_current_user
from app.models.user import User

router = APIRouter()

def _extract_json(content: str) -> Dict[str, Any]:
    """LLM 응답에서 JSON 객체를 추출합니다."""
    import json
    import re
    
    content = content.strip()
    
    # 1. 마크다운 코드 블록 제거
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0].strip()
    elif "```" in content:
        content = content.split("```")[1].split("```")[0].strip()
    
    # 2. JSON 파싱 시도
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
        
    # 3. 중괄호로 감싸진 부분만 추출하여 재시도 (설명 텍스트 제거)
    try:
        start_idx = content.find('{')
        end_idx = content.rfind('}')
        if start_idx != -1 and end_idx != -1:
            json_str = content[start_idx : end_idx + 1]
            return json.loads(json_str)
    except json.JSONDecodeError:
        pass
        
    # 4. 실패 시 빈 딕셔너리 또는 에러
    raise ValueError("Failed to extract JSON from response")

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
    
    system_prompt = (
        "당신은 드라마와 영화의 전문 시나리오 작가입니다. "
        "사용자가 제공한 상황과 인물 정보를 바탕으로, 몰입감 있고 드라마틱한 줄거리(Plot)와 배경(Background)을 **빠르게** 작성해주세요.\n\n"
        "[지침]\n"
        f"1. 반드시 주어진 인물 이름인 '{user_n}'(나/주인공)와 '{char_n}'(상대역)만 사용하세요.\n"
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
    
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"상황: {request.situation}"}
    ]
    
    try:
        import json as _json
        from app.core.config import settings
        
        # 사용자가 모델을 지정한 경우 해당 모델 사용, 아니면 Gemini 시도 후 로컬 LLM 폴백
        user_selected_model = request.model
        
        result = None
        
        if user_selected_model:
            # 사용자가 명시적으로 모델을 선택한 경우, 해당 모델만 사용
            import logging
            logging.info(f"Using user-selected model: {user_selected_model}")
            result = await call_llm(
                messages, 
                model=user_selected_model, 
                temperature=0.8, 
                max_tokens=4000,
                json_mode=False
            )
        else:
            # 모델 미지정: Gemini 시도 → 실패 시 로컬 LLM 폴백
            api_key = getattr(settings, "GEMINI_API_KEY", None)
            if not api_key:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Gemini API Key가 설정되지 않았습니다. .env 파일의 GOOGLE_API_KEY를 확인해주세요."
                )
            
            try:
                # 1차 시도: Gemini 2.5 Flash
                result = await call_llm(
                    messages, 
                    model="gemini-2.5-flash", 
                    temperature=0.8, 
                    max_tokens=4000,
                    json_mode=False
                )
            except Exception as gemini_error:
                # 2차 시도: Local Ollama (Gemma 3 27B)
                import logging
                logging.warning(f"Gemini API failed, switching to Local Ollama (gemma-3-27b): {gemini_error}")
                
                result = await call_llm(
                    messages,
                    model="gemma-3-27b",
                    temperature=0.8,
                    max_tokens=4000,
                    json_mode=False
                )
        
        content = result if isinstance(result, str) else result.get("content", "")
        
        # 태그 기반 파싱 ([줄거리] ... [배경] ...)
        plot = ""
        background = ""
        
        try:
            # 1. [줄거리]와 [배경] 태그 위치 찾기
            plot_idx = content.find("[줄거리]")
            bg_idx = content.find("[배경]")
            
            if plot_idx != -1 and bg_idx != -1:
                if plot_idx < bg_idx:
                    plot = content[plot_idx + 5 : bg_idx].strip()
                    background = content[bg_idx + 4 :].strip()
                else:
                    # 배경이 먼저 나온 경우 (혹시나)
                    background = content[bg_idx + 4 : plot_idx].strip()
                    plot = content[plot_idx + 5 :].strip()
            elif plot_idx != -1:
                # 줄거리만 있는 경우
                plot = content[plot_idx + 5 :].strip()
            elif bg_idx != -1:
                # 배경만 있는 경우 (특이케이스)
                background = content[:bg_idx].strip()
                plot = content[bg_idx + 4 :].strip()
            else:
                # 태그가 없는 경우 -> 전체를 줄거리로 간주
                plot = content.strip()
                
        except Exception as e:
            # 파싱 에러 시 전체를 plot으로
            import logging
            logging.warning(f"Parsing scenario failed: {e}")
            plot = content.strip()

        return {
            "success": True,
            "data": {
                "plot": plot,
                "background": background
            }
        }
    except Exception as e:
        # 에러 발생 시 Mock 응답 제공 (연결 실패 대비)
        import logging
        logging.error(f"Story generation failed, using mock: {e}")
        mock_story = (
            f"운명의 장난처럼 {request.situation} 상황이 펼쳐집니다. "
            "서로의 오해와 감정이 얽히며 예상치 못한 전개가 시작되려 합니다. "
            "이 긴장감 넘치는 순간, 당신의 선택이 모든 것을 결정할 것입니다."
        )
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
    
    # 1. 시스템 프롬프트 구성 (사용자 요청 사항 반영)
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
    
    user_input_parts = [
        f"이름: {request.name}",
        f"카테고리: {request.category or '미지정'}",
        f"작품명(출처): {request.source_work or '오리지널'}",
        f"기본설명: {request.description}"
    ]
    if request.worldview:
        user_input_parts.append(f"추가 세계관 설정: {request.worldview}")
        
    user_input = "\n".join(user_input_parts)
    
    messages = [
        {"role": "user", "content": user_input}
    ]
    
    is_fallback = False
    
    try:
        # 1차 시도: Gemini
        from app.core.config import settings
        api_key = getattr(settings, "GEMINI_API_KEY", None)
        if not api_key:
            raise ValueError("Gemini API Key missing")

        model_to_use = "gemini-2.5-flash"
        result = await call_llm(
            messages, 
            model=model_to_use, 
            temperature=0.7, 
            max_tokens=2000,
            json_mode=True,
            system_instruction=system_prompt
        )

    except Exception as gemini_error:
        # 2차 시도: Local Ollama (Gemma 3 27B)
        # 토큰 소진, 타임아웃, 키 누락 등 모든 에러 상황에서 폴백
        import logging
        logging.warning(f"Character generation: Gemini API failed/exhausted, switching to Local Ollama (gemma-3-27b). Error: {gemini_error}")
        
        is_fallback = True
        
        try:
            result = await call_llm(
                messages,
                model="gemma-3-27b",
                temperature=0.7,
                max_tokens=2000,
                json_mode=True,
                system_instruction=system_prompt
            )
        except Exception as ollama_error:
             raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Character generation failed (Gemini & Ollama): {str(ollama_error)}"
            )
    
    content = result if isinstance(result, str) else result.get("content", "")
    try:
        details = _extract_json(content)
        
        # likes/dislikes가 문자열로 온 경우 배열로 변환 처리 (Ollama 등 포맷 불안정 대비)
        if isinstance(details.get("likes"), str):
            details["likes"] = [x.strip() for x in details["likes"].split(",")]
        if isinstance(details.get("dislikes"), str):
            details["dislikes"] = [x.strip() for x in details["dislikes"].split(",")]
            
    except Exception as e:
         raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to parse generation result: {str(e)}"
        )
    
    return {
        "success": True,
        "is_fallback": is_fallback,
        "data": details
    }
            

