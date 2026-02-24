# 🎭 인생극장 (Screening Humanity) - Backend

> AI가 만들어내는 드라마 역할극 플랫폼 > AI 캐릭터와 함께 당신만의 시나리오를 완성하고, 직접 배우가 되거나 감독이 되어 극을 이끌어보세요.
<img width="1913" height="892" alt="스크린샷 2026-01-28 182236" src="https://github.com/user-attachments/assets/5873268a-9789-4a3e-a8cd-bbfcf05a2e53" />

---

##  주요 기능 (Key Features)

### 1. 두 가지 플레이 모드 
- **주연 모드 (Actor Mode):** 사용자가 직접 극의 주인공이 되어 AI 캐릭터와 1:1로 호흡을 맞추며 몰입감 있는 연기를 펼칩니다.
- **감독 모드 (Director Mode):** 두 명의 AI 배우를 무대에 배치하고, 제3자의 시선에서 대화의 흐름을 관찰하며 '연출 지시(Director's Note)'를 통해 극의 방향을 제어합니다.

### 2. 지능형 시나리오 생성
- 사용자가 입력한 단순한 키워드와 상황을 AI가 분석하여 **기승전결이 살아있는 드라마틱한 줄거리**로 변환합니다.
- **Google Gemini API**를 활용하여 입체적인 배경 설정과 갈등 요소를 자동으로 생성합니다.
<img width="1907" height="873" alt="스크린샷 2026-01-28 190239" src="https://github.com/user-attachments/assets/49132fe7-0c40-4250-af33-37296abd5986" />

### 3. 페르소나 기반 AI
- 캐릭터별 고유한 말투, 성격, 가치관이 완벽하게 반영된 대화 시스템을 제공합니다.
- **힌트 (Hints):** 대화가 막힐 때, 문맥을 분석하여 상황에 적절한 3가지 답변을 실시간으로 제안합니다.
<img width="1917" height="888" alt="스크린샷 2026-01-28 182353" src="https://github.com/user-attachments/assets/ee2aadec-3a4f-4a56-a97c-0a4574e2dc00" />

### 4. 실시간 멀티모달 경험 
- **TTS (Text-to-Speech):** 텍스트만으로는 느낄 수 없는 뉘앙스를 캐릭터 성격에 맞는 목소리로 전달합니다.
- 자동으로 흐름을 분석하여 대화를 이어나갑니다.
<img width="1897" height="577" alt="스크린샷 2026-01-28 190337" src="https://github.com/user-attachments/assets/d8053511-39f4-4295-b378-8b9b536aa54c" />

### 5. 연극 리뷰 및 분석 
- 극이 종료된 후, 상대 캐릭터의 시점에서 사용자의 연기를 평가합니다.
- 대화의 몰입도, 호감도 등을 수치화하여 제공하며, 전체 스토리를 한 줄로 요약해 줍니다.
<img width="539" height="426" alt="스크린샷 2026-01-28 190355" src="https://github.com/user-attachments/assets/e8aaed9d-01f9-4147-bf64-89dca62dd3fe" />

---
## 🛠️ 기술 스택 (Tech Stack)

*   **Language:** Python 3.11+
*   **Framework:** FastAPI
*   **Database:** SQLite (개발용) / SQLAlchemy (ORM)
*   **AI & LLM:**
    *   Google Gemini API (Generative AI)
    *   Ollama (Local LLM Support)
*   **Authentication:** OAuth2 (Google), JWT
*   **ETC:** HTTPX (Async Client), Pydantic

## 🔐 Gemini 키 운영 정책

*   Gemini 비밀키는 **Backend `.env`에서만** 관리합니다.
*   권장 키: `GEMINI_API_KEY`
*   Gemini 기본 모델: `GOOGLE_API_MODEL` (예: `gemini-2.5-flash`, `gemini-2.5-pro`)
*   레거시 별칭(선택): `GOOGLE_API_KEY`
*   금지: `NEXT_PUBLIC_GEMINI_API_KEY` (FRONT 공개 env, 사용 금지/폐기)
*   Gemini Safety 필터 강도 조절: `GOOGLE_SAFETY_THRESHOLD` (`BLOCK_NONE`, `BLOCK_ONLY_HIGH`, `BLOCK_MEDIUM_AND_ABOVE`, `BLOCK_LOW_AND_ABOVE`, `HARM_BLOCK_THRESHOLD_UNSPECIFIED`)
*   관련 설정 문서: `env 설정법.md`

## ✨ 주요 기능 (Features)

*   **사용자 인증 (Auth)**
    *   Google OAuth 소셜 로그인
    *   Guest 로그인 (백엔드 guest 세션 + HttpOnly guest 쿠키)
    *   JWT 액세스 토큰 발급 및 관리
    *   인증 감사 로그(`auth_events`) / guest 세션 기록(`guest_sessions`)
    *   개발 편의를 위한 `dev-user` 자동 로그인 지원
*   **캐릭터 관리 (Character)**
    *   나만의 페르소나 캐릭터 생성 (이미지, 성격, 말투 등)
    *   AI 기반 캐릭터 상세 설정 자동 생성 (Gemini 활용)
    *   사전 설정(Preset) 캐릭터 제공
*   **AI 스토리 및 채팅 (Story & Chat)**
    *   상황극 스토리 자동 생성
    *   캐릭터와 실시간 채팅
    *   LLM 모델 폴백 시스템 (Gemini 장애 시 로컬 LLM 전환)
*   **음성 모델 (Voice/TTS)**
    *   GPT-SoVITS 기반 음성 모델 학습 및 합성 연동 (외부 GPU 서버 연동)
    *   음성 모델 관리 및 테스트

## 📂 프로젝트 구조

```
app/
├── api/            # API 엔드포인트 라우터 (auth, chat, characters 등)
├── core/           # 핵심 설정 (config, database, llm 등)
├── models/         # SQLAlchemy 데이터베이스 모델
├── services/       # 비즈니스 로직 (context, tts_queue 등)
└── workers/        # 백그라운드 워커 (현재 비활성화됨)
docs/               # 프로젝트 문서
```

## ⚙️ 환경변수 안내

*   Backend env 템플릿: `.env.example`
*   자세한 설명: `env 설정법.md`
*   루트 Docker Compose 사용 시 일부 URL은 루트 `docker-compose*.yaml`에서 override될 수 있습니다.

### 1차 보안 가시화 플래그 (기본값은 기존 동작 유지)

*   `DEV_AUTH_FALLBACK_ENABLED` : 개발용 `dev-user` 인증 폴백 허용 여부 (운영은 `false` 권장)
*   `GOOGLE_SSO_ALLOW_INSECURE_HTTP` : 로컬 HTTP OAuth 테스트 허용 여부 (운영 HTTPS는 `false` 권장)
*   `AUTH_COOKIE_SECURE` / `AUTH_COOKIE_SAMESITE` : 인증 쿠키 보안 속성
*   `STARTUP_SCHEMA_PATCH_ENABLED` : startup ad-hoc DB 패치 실행 여부 (레거시 호환용)
*   `APP_ENV` + `STRICT_STARTUP_VALIDATION` : 운영 모드에서 위험 설정 조합 fail-fast 검증
*   `GUEST_AUTH_ENABLED`, `GUEST_AUTH_COOKIE_NAME`, `GUEST_ACCESS_TOKEN_EXPIRE_MINUTES`, `GUEST_COOKIE_SESSION_ONLY` : guest 인증 정책 제어

### Docker GPT-SoVITS 데이터 볼륨 정책 (루트 compose)

*   루트 `docker-compose.yaml`에서 GPT-SoVITS 관련 볼륨은 Docker named volume 대신 Windows bind mount로 연결됩니다.
*   기본 경로: `D:/docker/madcamp03/gpt-sovits/*`
*   루트 `.env`에서 `DOCKER_DATA_ROOT`로 override 가능 (예: `DOCKER_DATA_ROOT=D:/docker/madcamp03`)


