"""
Voice 모델
GPT-SoVITS 참조 오디오 정보를 저장합니다.
"""
from sqlalchemy import Column, String, DateTime, Boolean, Text, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
import uuid
from app.core.database import Base


class Voice(Base):
    """
    음성 모델 - GPT-SoVITS 참조 오디오 관리
    
    각 음성은 GPT-SoVITS에서 TTS 생성 시 사용되는 
    참조 오디오(ref_audio) 정보를 포함합니다.

    [주의]
    - `ref_audio_path`, weights 경로는 Server A 기준 경로 문자열이며 백엔드 로컬 경로와 다를 수 있다.
    - `train_input_dir`/`training_model_name`은 model-make 정리/삭제 흐름과 연결되어 있어 의미 변경에 주의.
    """
    __tablename__ = "voices"

    # 기본 식별자
    id = Column(String(36), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    
    # 음성 기본 정보
    name = Column(String(100), nullable=False)  # 음성 이름 (예: "차분한 남성", "밝은 여성")
    description = Column(Text, nullable=True)   # 음성 설명
    language = Column(String(10), default="ko") # 언어 코드 (ko, en, ja, zh 등)
    
    # GPT-SoVITS 필수 설정
    ref_audio_path = Column(String(500), nullable=False)  # Server A 내부 참조 오디오 경로
    prompt_text = Column(Text, nullable=True, default="")  # 참조 오디오의 텍스트 (정확도 향상용)
    prompt_lang = Column(String(10), default="ko")         # 참조 오디오 언어
    
    # GPT-SoVITS Fine-tuned 모델 설정 (선택)
    gpt_weights_path = Column(String(500), nullable=True)    # GPT 모델 경로 (예: /opt/GPT-SoVITS/GPT_weights_v2/xxx.ckpt)
    sovits_weights_path = Column(String(500), nullable=True) # SoVITS 모델 경로 (예: /opt/GPT-SoVITS/SoVITS_weights_v2/xxx.pth)
    model_version = Column(String(20), default="v2")         # 모델 버전 (v2, v3, v4 등)
    
    # 훈련 음성 정보 (sample_train_voice 연결)
    train_voice_folder = Column(String(200), nullable=True)  # 훈련 음성 폴더명 (예: "makima", "frieren")
    # 모델 제작(model-make) 업로드 시 Server A 경로 및 학습 모델명
    train_input_dir = Column(String(500), nullable=True)   # 업로드 WAV가 저장된 Server A 디렉터리 (예: user_{id}/run_{ts})
    training_model_name = Column(String(200), nullable=True)  # 학습 시 사용한 model_name (logs/{model_name} 삭제용)
    
    # 메타데이터
    is_default = Column(Boolean, default=False)  # 기본 음성 여부
    is_active = Column(Boolean, default=True)    # 활성화 상태 (비활성화 시 목록에서 숨김)
    
    # 소유자 (None이면 시스템/관리자 음성)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    
    # 타임스탬프
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())
    
    # 관계 설정 (캐릭터와 연동)
    characters = relationship("Character", back_populates="voice")
    user = relationship("User", back_populates="voices")
    
    def __repr__(self):
        return f"<Voice(id={self.id}, name={self.name}, language={self.language})>"
