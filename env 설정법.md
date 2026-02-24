# BACK `env 설정법`

이 문서는 `madcamp-Screening-Humanity-BACK`의 `.env` 설정 방법을 정리합니다.

핵심 원칙:
- URL/도메인 설정의 단일 진실 원천은 `app/core/config.py`의 `Settings`
- 라우트 코드에서 `localhost` fallback 하드코딩하지 않음
- Docker/로컬 실행 모두 `.env`를 기준으로 하고, 루트 compose가 일부 값을 override 가능
- Gemini 비밀키는 **Backend `.env`의 `GEMINI_API_KEY`** 로만 관리 (FRONT `NEXT_PUBLIC_*` 사용 금지)

## 1. 어디에 작성하나

파일 위치:
- `madcamp-Screening-Humanity-BACK/.env`

읽는 위치:
- `madcamp-Screening-Humanity-BACK/app/core/config.py` (`env_file=".env"`)

## 2. 우선순위 (실무 기준)

1. 루트 `docker-compose.yaml`의 `environment` override (Docker 실행 시)
2. `madcamp-Screening-Humanity-BACK/.env`
3. `app/core/config.py` 기본값

즉:
- Docker로 띄우면 일부 값은 compose가 덮어씀 (`SERVER_A_*`, `TTS_BASE_URL` 등)
- 로컬 직접 실행(`uvicorn`)이면 `.env` 값이 그대로 사용됨

## 3. 최소 필수 env (로컬 개발 기준)

아래는 "일단 서버가 뜨고 주요 기능이 도는" 기준 최소값입니다.

```env
# 보안/JWT (반드시 변경 권장)
SECRET_KEY=CHANGE_ME_TO_A_LONG_RANDOM_SECRET

# 프론트/백엔드 공개 주소
FRONTEND_URL=http://localhost:3000
BACKEND_CORS_ORIGINS=["http://localhost:3000"]
GOOGLE_REDIRECT_URI=http://localhost:8000/api/auth/google/callback

# Server A (GPT-SoVITS 자동화 API / TTS)
SERVER_A_FILES_API_URL=http://localhost:10001
SERVER_A_TRAINING_API_URL=http://localhost:10002
TTS_BASE_URL=http://localhost:9880

# LLM (로컬 Ollama 예시)
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_API_PATH=/api/chat

# Redis (없으면 일부 큐/스트리밍 기능 제한 가능)
REDIS_URL=redis://localhost:6379/0
```

## 4. 자주 쓰는 핵심 env 설명

## 프론트/인증/도메인

### `FRONTEND_URL`
- OAuth 로그인 후 리다이렉트/프론트 복귀용 기준 주소
- 예시(로컬): `http://localhost:3000`
- 예시(배포): `https://app.example.com`

### `BACKEND_CORS_ORIGINS`
- CORS 허용 origin 목록
- 형식 1 (권장 JSON 배열):
```env
BACKEND_CORS_ORIGINS=["http://localhost:3000","https://app.example.com"]
```
- 형식 2 (쉼표 구분 문자열):
```env
BACKEND_CORS_ORIGINS=http://localhost:3000,https://app.example.com
```

### `GOOGLE_REDIRECT_URI`
- Google OAuth 콜백 URL
- 반드시 Google Cloud Console에 등록한 값과 일치해야 함

### `GOOGLE_SSO_ALLOW_INSECURE_HTTP`
- Google OAuth 라이브러리에서 HTTP(비HTTPS) 콜백 허용 여부
- 로컬 개발 편의를 위해 기본값 `true` 유지
- 운영(HTTPS)에서는 `false` 권장

### `AUTH_COOKIE_NAME`
- 인증 쿠키 이름
- 기본값 `access_token`
- 특별한 이유가 없으면 변경하지 않는 것을 권장

### `AUTH_COOKIE_SECURE`
- 인증 쿠키의 `Secure` 속성
- 로컬 HTTP 개발: `false`
- 운영 HTTPS: `true` 권장

### `AUTH_COOKIE_SAMESITE`
- 인증 쿠키 SameSite 정책 (`lax` / `strict` / `none`)
- 현재 기본값은 기존 호환을 위해 `lax`

### `APP_ENV`
- 앱 실행 환경 구분값 (`development` / `staging` / `production`)
- `STRICT_STARTUP_VALIDATION=true`와 함께 운영 안전성 검증 정책에 사용
- 예시: `APP_ENV=production`

### `DEV_AUTH_FALLBACK_ENABLED`
- 토큰 검증 실패/미존재 시 `dev-user` 폴백 허용 여부
- 로컬 개발 편의를 위해 기본값 `true`
- 운영에서는 `false` 권장 (보안상 중요)

### `GUEST_AUTH_ENABLED`
- 백엔드 guest 로그인 엔드포인트(`/api/auth/guest/login`) 활성화 여부
- 기본값 `true`
- 운영 정책상 guest 사용을 막고 싶으면 `false`

### `GUEST_AUTH_COOKIE_NAME`
- guest 인증용 HttpOnly 쿠키 이름
- 기본값 `guest_access_token`
- Google user 쿠키(`AUTH_COOKIE_NAME`)와 분리되어 충돌 방지

### `GUEST_ACCESS_TOKEN_EXPIRE_MINUTES`
- guest JWT 만료 시간(분)
- 기본값 `1440` (1일)

### `GUEST_COOKIE_SESSION_ONLY`
- guest 쿠키를 브라우저 세션 쿠키로 발급할지 여부
- `true`면 `max_age` 없이 발급되어 브라우저 세션 종료 시 삭제
- 프론트 탭 세션(`sessionStorage`)과는 별개 정책임

### `STRICT_STARTUP_VALIDATION`
- startup 시 설정/운영 경고를 엄격 검증(실패로 처리)할지 여부
- 기본값 `false`
- `APP_ENV=production`에서 일부 위험 조합(예: `DEV_AUTH_FALLBACK_ENABLED=true`)은 fail-fast로 차단될 수 있음

### `STARTUP_SCHEMA_PATCH_ENABLED`
- `app/main.py` startup 단계에서 레거시 DB 호환용 ad-hoc DDL 패치를 실행할지 여부
- 현재는 기존 동작 호환을 위해 기본값 `true`
- 장기적으로는 Alembic 등 정식 마이그레이션 도구로 대체하는 것이 목표

## LLM / TTS / Server A

### `OLLAMA_BASE_URL`
- Ollama 서버 기본 주소
- 예시: `http://localhost:11434`, `https://ollama.example.com`

### `OLLAMA_API_PATH`
- 기본: `/api/chat`
- 프록시 경로가 끼면 `/ollama/api/chat` 같은 값 가능

### `TTS_BASE_URL`
- GPT-SoVITS TTS API 기본 주소
- 예시(로컬 docker): `http://localhost:9880`
- 예시(compose 내부 override): `http://gpt-sovits-cu128:9880`
- 예시(배포): `https://tts.example.com`

### `SERVER_A_FILES_API_URL`
- GPT-SoVITS 파일 스캔/업로드 API (`10001`)

### `SERVER_A_TRAINING_API_URL`
- GPT-SoVITS 학습 API (`10002`)

## 경로 관련 (Server A 파일 조작)

### `SERVER_A_TRAIN_VOICE_ROOT`
### `SERVER_A_LOGS_ROOT`
### `SERVER_A_TEMP_ROOT`
- Host/Conda 운영일 때는 `/opt/GPT-SoVITS/...`
- Docker 연동일 때 루트 compose가 `/workspace/GPT-SoVITS/...`로 override 가능

## 5. 추가/선택 env (이번에 반영된 항목 포함)

### `BACKEND_PUBLIC_URL` (선택)
- 공개 백엔드 주소를 별도 보관할 때 사용
- 현재 필수 아님 (비워도 됨)
- 운영 문서/로깅/후속 확장 대비용

### `SERVER_A_STACK_MODE` (선택)
- 값 예시: `docker`, `host`
- 운영 모드 표기용 (문서/로그 기준)
- 현재 비워도 동작 영향 없음

## 6. 기타 자주 쓰는 env (프로젝트 상황에 따라)

```env
# DB
DATABASE_URL=sqlite+aiosqlite:///./avatar_forge.db

# Google OAuth
GOOGLE_CLIENT_ID=REPLACE_ME
GOOGLE_CLIENT_SECRET=REPLACE_ME

# AI Keys (백엔드에서 사용할 경우 권장)
GEMINI_API_KEY=REPLACE_ME
GOOGLE_API_MODEL=gemini-2.5-flash
# GOOGLE_API_KEY=REPLACE_ME  # legacy alias (선택)
GOOGLE_SAFETY_THRESHOLD=BLOCK_NONE
OPENAI_API_KEY=

# 관리자 이메일 (쉼표 구분)
ADMIN_EMAILS=admin@example.com,manager@example.com

# 파일 저장 경로
SHARED_MODELS_DIR=/mnt/shared_models
USER_ASSETS_DIR=/mnt/user_assets
```

## 7. 로컬 개발용 예시 (`.env`)

```env
PROJECT_NAME=Avatar Forge Backend
API_V1_STR=/api

SECRET_KEY=CHANGE_ME_TO_A_LONG_RANDOM_SECRET
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=10080

DATABASE_URL=sqlite+aiosqlite:///./avatar_forge.db

FRONTEND_URL=http://localhost:3000
BACKEND_CORS_ORIGINS=["http://localhost:3000"]
GOOGLE_REDIRECT_URI=http://localhost:8000/api/auth/google/callback
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_SSO_ALLOW_INSECURE_HTTP=true

APP_ENV=development

AUTH_COOKIE_NAME=access_token
AUTH_COOKIE_SECURE=false
AUTH_COOKIE_SAMESITE=lax
DEV_AUTH_FALLBACK_ENABLED=true
GUEST_AUTH_ENABLED=true
GUEST_AUTH_COOKIE_NAME=guest_access_token
GUEST_ACCESS_TOKEN_EXPIRE_MINUTES=1440
GUEST_COOKIE_SESSION_ONLY=true

STRICT_STARTUP_VALIDATION=false
STARTUP_SCHEMA_PATCH_ENABLED=true

GEMINI_API_KEY=
GOOGLE_API_MODEL=gemini-2.5-flash
GOOGLE_SAFETY_THRESHOLD=BLOCK_NONE
OPENAI_API_KEY=

LLM_SERVICE=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_API_PATH=/api/chat
OLLAMA_SSL_VERIFY=false

TTS_BASE_URL=http://localhost:9880
TTS_API_PATH=tts
TTS_SSL_VERIFY=false

SERVER_A_FILES_API_URL=http://localhost:10001
SERVER_A_TRAINING_API_URL=http://localhost:10002
SERVER_A_TRAIN_VOICE_ROOT=/opt/GPT-SoVITS/sample_train_voice
SERVER_A_LOGS_ROOT=/opt/GPT-SoVITS/logs
SERVER_A_TEMP_ROOT=/opt/GPT-SoVITS/TEMP

REDIS_URL=redis://localhost:6379/0
```

## 8. Docker Compose로 실행할 때 주의

루트 `docker-compose.yaml`는 아래 값을 override할 수 있습니다:
- `SERVER_A_FILES_API_URL`
- `SERVER_A_TRAINING_API_URL`
- `TTS_BASE_URL`

즉 `.env`에 외부/duckdns 값을 넣어둬도, 로컬 Docker 실행에서는 compose가 내부 컨테이너 주소로 덮어쓸 수 있습니다.

## 9. 빠른 체크리스트

- CORS 에러가 나면 `BACKEND_CORS_ORIGINS` 형식(JSON 배열/쉼표)을 먼저 확인
- OAuth가 실패하면 `GOOGLE_REDIRECT_URI`와 `FRONTEND_URL`이 실제 도메인과 맞는지 확인
- OAuth 로컬 테스트가 갑자기 안 되면 `GOOGLE_SSO_ALLOW_INSECURE_HTTP` 값 확인 (`http://` 환경에서는 true 필요)
- 로그인 없이도 API가 열리는 것처럼 보이면 `DEV_AUTH_FALLBACK_ENABLED=true` 상태인지 확인 (운영에서는 false 권장)
- HTTPS 운영인데 쿠키가 안 붙으면 `AUTH_COOKIE_SECURE=true` / `AUTH_COOKIE_SAMESITE` 조합 확인
- 학습/TTS 호출이 실패하면 `SERVER_A_*_URL`, `TTS_BASE_URL` 확인
- 로컬/도커 경로 혼용 시 `SERVER_A_*_ROOT` 값이 운영 방식과 맞는지 확인
- Gemini 비밀키는 `madcamp-Screening-Humanity-BACK/.env`의 `GEMINI_API_KEY`에만 넣었는지 확인 (`NEXT_PUBLIC_GEMINI_API_KEY` 사용 금지)
- Gemini 기본 모델을 바꾸려면 `GOOGLE_API_MODEL` 값을 올바른 Gemini 모델명으로 넣었는지 확인
- Gemini Safety 필터 강도를 바꾸려면 `GOOGLE_SAFETY_THRESHOLD` 값을 허용 enum 이름으로 넣었는지 확인

## 10. `GOOGLE_API_MODEL` 설정값 설명

`GOOGLE_API_MODEL`은 Backend가 Gemini를 호출할 때 기본으로 사용할 모델명입니다.

사용 위치:
- `app/core/llm.py`에서 `model` 미지정 시 기본값
- `app/api/ai.py`의 Gemini 우선 시도 경로
- `app/api/characters.py`의 캐릭터 상세 생성 Gemini 시도 경로

예시:
- `gemini-2.5-flash`
- `gemini-2.5-pro`

현재 프로젝트 기본값(호환성 유지):
- `GOOGLE_API_MODEL=gemini-2.5-flash`

## 11. `GOOGLE_SAFETY_THRESHOLD` 설정값 설명

`GOOGLE_SAFETY_THRESHOLD`는 Gemini Safety 필터 강도를 조절하는 값입니다.  
`google-generativeai`의 `HarmBlockThreshold` enum 이름을 그대로 사용합니다.

허용값:
- `BLOCK_NONE`
- `BLOCK_ONLY_HIGH`
- `BLOCK_MEDIUM_AND_ABOVE`
- `BLOCK_LOW_AND_ABOVE`
- `HARM_BLOCK_THRESHOLD_UNSPECIFIED`

의미(실무적으로 이해하면):
- `BLOCK_NONE`: 어떤 위해성 있는 문장도 거르지 않겠다 (가장 완화)
- `BLOCK_ONLY_HIGH`: 높은 위해성만 차단
- `BLOCK_MEDIUM_AND_ABOVE`: 중간 이상 위해성 차단
- `BLOCK_LOW_AND_ABOVE`: 조금이라도 위해성이 있으면 차단 (가장 엄격)
- `HARM_BLOCK_THRESHOLD_UNSPECIFIED`: SDK/서비스 기본값 사용

현재 프로젝트 기본값(호환성 유지):
- `GOOGLE_SAFETY_THRESHOLD=BLOCK_NONE`
