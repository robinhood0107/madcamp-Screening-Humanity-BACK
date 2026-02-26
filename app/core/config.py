from typing import List, Union, Optional
from pydantic import AnyHttpUrl, validator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    PROJECT_NAME: str = "Avatar Forge Backend"
    API_V1_STR: str = "/api"
    APP_ENV: str = "development"
    
    # CORS
    BACKEND_CORS_ORIGINS: List[AnyHttpUrl] = ["http://localhost:3000"]

    @validator("BACKEND_CORS_ORIGINS", pre=True)
    def assemble_cors_origins(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str):
            # JSON 배열 형식 파싱: ["http://localhost:3000","http://localhost:8000"]
            if v.startswith("["):
                import json
                try:
                    return json.loads(v)
                except json.JSONDecodeError:
                    # JSON 파싱 실패 시 쉼표로 분리
                    return [i.strip().strip('"').strip("'") for i in v.strip("[]").split(",") if i.strip()]
            # 쉼표로 구분된 문자열: http://localhost:3000,http://localhost:8000
            return [i.strip() for i in v.split(",") if i.strip()]
        elif isinstance(v, list):
            return [str(origin) for origin in v]
        return []

    # Database
    # Default to sqlite for dev if postgres not provided
    DATABASE_URL: str = "sqlite+aiosqlite:///./avatar_forge.db" 
    # 데이터 계층 분리 전환 토글 (C gRPC data-service 사용)
    USE_C_DATA_SERVICE: bool = False
    DATA_SERVICE_GRPC_ADDR: str = "localhost:50051"
    DATA_SERVICE_TIMEOUT_MS: int = 5000
    # gRPC 툴체인이 없는 환경에서 c-data-service 바이너리 one-shot 명령으로
    # rehearsal 경로를 확인하기 위한 임시 브리지 (Phase C 중간 단계).
    DATA_SERVICE_CLI_BRIDGE_ENABLED: bool = False
    DATA_SERVICE_CLI_BIN: str = "c-data-service"
    # 리허설 단계에서는 gRPC 실패 시 Python 브리지 fallback을 허용한다.
    # 컷오버 리허설 후반/운영 검증에서는 false로 내려 fallback 없이 실패하게 만들어
    # 실제 gRPC 경유 성공 여부를 강제 검증할 수 있다.
    DATA_SERVICE_REHEARSAL_FALLBACK_ENABLED: bool = True

    # 하이브리드 저장 정책 (향후 C data-service/스토리지 계층에서 사용)
    MEDIA_STORAGE_ROOT: str = "/mnt/media_assets"
    MODEL_STORAGE_ROOT: str = "/mnt/models"
    BLOB_MAX_BYTES_DEFAULT: int = 16 * 1024 * 1024
    BLOB_MAX_BYTES_AUDIO: int = 64 * 1024 * 1024
    # Phase E 컷오버 준비: backend가 파일 기반 preset fallback을 사용할지 제어
    # (운영/리허설 후반에는 False로 내려 DB preset source 단일화를 강제)
    CHARACTER_FILE_PRESET_FALLBACK_ENABLED: bool = True

    # JWT
    SECRET_KEY: str = "YOUR_SECRET_KEY_HERE_CHANGE_IN_PROD"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days

    # Google OAuth
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/api/auth/google/callback"
    # [보안 플래그] 로컬 개발 환경에서만 True를 권장.
    # 운영(HTTPS)에서는 False로 내려야 Google OAuth 흐름이 더 안전해진다.
    GOOGLE_SSO_ALLOW_INSECURE_HTTP: bool = True
    
    # AI API Keys
    GEMINI_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    # Gemini 기본 모델 (Backend에서 Gemini 호출 시 기본값으로 사용)
    GOOGLE_API_MODEL: str = "gemini-2.5-flash"
    # Gemini Safety Threshold (google-generativeai HarmBlockThreshold enum name)
    # 예: BLOCK_NONE / BLOCK_ONLY_HIGH / BLOCK_MEDIUM_AND_ABOVE / BLOCK_LOW_AND_ABOVE / HARM_BLOCK_THRESHOLD_UNSPECIFIED
    GOOGLE_SAFETY_THRESHOLD: str = "BLOCK_NONE"
    
    @validator("GEMINI_API_KEY", pre=True, always=True)
    def set_gemini_api_key(cls, v, values):
        import os
        import warnings
        if v:
            return v
        if os.environ.get("NEXT_PUBLIC_GEMINI_API_KEY"):
            warnings.warn(
                "NEXT_PUBLIC_GEMINI_API_KEY is ignored by security policy. "
                "Move the Gemini secret to Backend .env as GEMINI_API_KEY (or GOOGLE_API_KEY legacy alias).",
                RuntimeWarning,
            )
        # 여러 환경 변수 이름 시도
        return (
            os.getenv("GEMINI_API_KEY") or 
            os.getenv("GOOGLE_API_KEY") or 
            ""
        )
    
    # Frontend URL (OAuth 콜백 리다이렉트용)
    FRONTEND_URL: str = "http://localhost:3000"
    BACKEND_PUBLIC_URL: str = ""  # 선택값: 비어 있으면 GOOGLE_REDIRECT_URI 사용
    
    # Auth Cookie / 보안 동작 (1차 리팩토링: 기본값은 기존 동작 유지)
    # 여기 값들을 둔 이유:
    # - auth.py에 하드코딩돼 있던 보안 민감 옵션을 설정으로 끌어올려서
    #   운영에서 코드 수정 없이 조정 가능하게 하려는 목적이다.
    AUTH_COOKIE_NAME: str = "access_token"
    AUTH_COOKIE_SECURE: bool = False
    AUTH_COOKIE_SAMESITE: str = "lax"
    DEV_AUTH_FALLBACK_ENABLED: bool = True  # 토큰 실패 시 dev-user 폴백 (운영에서는 False 권장)
    STRICT_STARTUP_VALIDATION: bool = False  # 1차는 warn-only 정책
    STARTUP_SCHEMA_PATCH_ENABLED: bool = True  # main.py의 ad-hoc DDL 패치 토글
    GUEST_AUTH_ENABLED: bool = True
    GUEST_AUTH_COOKIE_NAME: str = "guest_access_token"
    GUEST_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 1 day
    GUEST_COOKIE_SESSION_ONLY: bool = True
    # Google 로그인 후 guest→Google 병합 모달을 띄우기 위해 guest 쿠키를 잠시 유지한다.
    # 최종 정리는 /api/auth/guest/merge 또는 /api/auth/logout에서 수행.
    PRESERVE_GUEST_COOKIE_ON_GOOGLE_LOGIN: bool = True

    # LLM 서비스 설정
    # "vllm" 또는 "ollama" 중 선택 (동시 실행 불가, VRAM 제약)
    # 현재 기본값: ollama (vLLM 코드는 주석 처리되어 있음)
    LLM_SERVICE: str = "ollama"
    
    # vLLM API 설정 (OpenAI 호환)
    # 엔드포인트: /v1/chat/completions
    # NPM 프록시 예시: https://llm.server-a.local
    # 직접 접근 예시: http://server-a:8002
    # VLLM_BASE_URL: str = "http://localhost:8002"  # vLLM 서비스 기본 URL (포트 8002)
    
    # Ollama API 설정
    # 엔드포인트: /api/chat
    # NPM 프록시 예시: https://ollama.server-a.local 또는 http://gpugpt.duckdns.org
    # 직접 접근 예시: http://server-a:11434
    OLLAMA_BASE_URL: str = "http://gpugpt.duckdns.org"  # Ollama 서비스 기본 URL (리버스 프록시 사용)
    OLLAMA_API_PATH: str = "/api/chat"  # Ollama API 경로 (리버스 프록시 경로 포함 가능, 예: "/ollama/api/chat")
    OLLAMA_SSL_VERIFY: bool = False  # SSL 인증서 검증 (개발 환경: False, 프로덕션: True)
    
    # 컨텍스트/슬라이딩 윈도우 (Gemma 3 27B, Ollama n_ctx=4096)
    # 4k 컨텍스트에서 시스템 프롬프트(~1.5~2k) + 메시지(~1.5k) + 응답 버퍼(~0.5k) 확보
    CONTEXT_WINDOW_TURNS: int = 3  # 1턴=user+assistant 1쌍, 3턴=6개 메시지 (약 600~900 토큰)
    CONTEXT_MAX_TOKENS: int = 4000  # 4k 모델 한계
    CONTEXT_TOKEN_THRESHOLD_RATIO: float = 0.75  # 75% (3000) 초과 시 요약 트리거
    
    # GPT-SoVITS TTS API 설정
    # 엔드포인트: /tts (POST/GET)
    # NPM 프록시 예시: https://tts.server-a.local
    # 직접 접근 예시: http://server-a:9880
    TTS_BASE_URL: str = "http://gpusovitsapi.duckdns.org"  # GPT-SoVITS TTS 서비스 기본 URL (포트 9880, api_v2.py)
    TTS_API_PATH: str = "tts"  # TTS API 경로 (프록시 경로 포함 가능, 예: "tts/tts")
    TTS_TIMEOUT: float = 120.0  # TTS API 호출 타임아웃 (초)
    TTS_MAX_TEXT_LENGTH: int = 10000  # 텍스트 길이 제한 (자)
    TTS_MAX_FILE_SIZE: int = 52428800  # 생성된 오디오 파일 크기 제한 (바이트, 기본 50MB)
    TTS_SSL_VERIFY: bool = False  # SSL 인증서 검증 (개발 환경: False, 프로덕션: True)
    
    # 관리자 설정
    # 관리자 권한이 있는 이메일 목록 (쉼표로 구분)
    # 예: "admin@example.com,manager@example.com"
    ADMIN_EMAILS: str = ""
    
    def is_admin(self, email: str) -> bool:
        """이메일이 관리자 목록에 있는지 확인"""
        if not self.ADMIN_EMAILS:
            return False
        admin_list = [e.strip().lower() for e in self.ADMIN_EMAILS.split(",") if e.strip()]
        return email.lower() in admin_list

    @validator("AUTH_COOKIE_SAMESITE", pre=True)
    def normalize_auth_cookie_samesite(cls, v: Optional[str]) -> str:
        """
        samesite 값을 FastAPI/Starlette가 기대하는 소문자 문자열로 정규화.
        잘못된 값이 들어와도 기존 동작(lax)으로 폴백해서 서버 부팅이 깨지지 않게 유지한다.
        """
        raw = (v or "lax").strip().lower()
        if raw not in {"lax", "strict", "none"}:
            return "lax"
        return raw
    
    # Server A 파일 스캔 API (GPT-SoVITS 모델/음성 파일 조회용)
    # 프록시 사용 시 포트 없이 gpufilemanager.duckdns.org, /api/health·/api/files/* 라우팅
    SERVER_A_FILES_API_URL: str = "http://gpufilemanager.duckdns.org"

    # Server A 학습 API (GPT-SoVITS 모델 학습용). 프록시: gpuvoicetrain.duckdns.org
    SERVER_A_TRAINING_API_URL: str = "http://gpuvoicetrain.duckdns.org"
    SERVER_A_STACK_MODE: str = ""  # 선택값: docker|host (문서/운영 로깅용)

    # Server A 경로 (model-make 업로드/삭제용). file_scanner_api·training_api와 동일한 값 사용
    SERVER_A_TRAIN_VOICE_ROOT: str = "/opt/GPT-SoVITS/sample_train_voice"
    SERVER_A_LOGS_ROOT: str = "/opt/GPT-SoVITS/logs"
    SERVER_A_TEMP_ROOT: str = "/opt/GPT-SoVITS/TEMP"  # 학습 중간 TEMP (abort 시 삭제)
    
    # Paths (Configurable for Windows/Ubuntu)
    SHARED_MODELS_DIR: str = "/mnt/shared_models"
    USER_ASSETS_DIR: str = "/mnt/user_assets"

    # Redis (TTS Queue & Stream)
    # Docker Compose: redis://redis:6379/0
    # Local: redis://localhost:6379/0
    REDIS_URL: str = "redis://localhost:6379/0"
    
    # TTS Queue Settings
    TTS_QUEUE_MAX_SIZE: int = 100
    TTS_QUEUE_JOB_TIMEOUT: int = 60  # 초
    TTS_RESULT_TTL: int = 300  # 5분

    @staticmethod
    def _rstrip_slash(url: str) -> str:
        return (url or "").rstrip("/")

    @property
    def backend_cors_allow_origins(self) -> List[str]:
        origins = [str(origin).rstrip("/") for origin in self.BACKEND_CORS_ORIGINS]
        return origins or ["http://localhost:3000"]

    @property
    def frontend_base_url(self) -> str:
        return self._rstrip_slash(self.FRONTEND_URL or "http://localhost:3000")

    @property
    def auth_cookie_name(self) -> str:
        return (self.AUTH_COOKIE_NAME or "access_token").strip() or "access_token"

    @property
    def guest_auth_cookie_name(self) -> str:
        return (self.GUEST_AUTH_COOKIE_NAME or "guest_access_token").strip() or "guest_access_token"

    @property
    def auth_cookie_samesite_value(self) -> str:
        return (self.AUTH_COOKIE_SAMESITE or "lax").strip().lower() or "lax"

    @property
    def app_env_normalized(self) -> str:
        return (self.APP_ENV or "development").strip().lower() or "development"

    @property
    def server_a_files_api_base_url(self) -> str:
        return self._rstrip_slash(self.SERVER_A_FILES_API_URL or "http://localhost:10001")

    @property
    def server_a_training_api_base_url(self) -> str:
        return self._rstrip_slash(self.SERVER_A_TRAINING_API_URL or "http://localhost:10002")

    @property
    def redis_url_value(self) -> str:
        return (self.REDIS_URL or "redis://localhost:6379/0").strip()

    @property
    def data_service_grpc_addr_value(self) -> str:
        return (self.DATA_SERVICE_GRPC_ADDR or "localhost:50051").strip()

    @property
    def data_service_cli_bin_value(self) -> str:
        return (self.DATA_SERVICE_CLI_BIN or "c-data-service").strip() or "c-data-service"

    model_config = SettingsConfigDict(
        env_file=".env", 
        case_sensitive=True,
        extra="ignore"
    )

settings = Settings()
