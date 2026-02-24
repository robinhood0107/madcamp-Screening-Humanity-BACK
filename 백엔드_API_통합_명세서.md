# BACK 백엔드 API 통합 명세서

상태: `현재 기준 문서` (코드 + `백엔드_함수_분석_및_역할_분석_명세서.md` 기준 재구성)

- 작성일: 2026-02-24
- 수정일: 2026-02-24
- 대상 프로젝트: `madcamp-Screening-Humanity-BACK`
- 기준 소스 문서: `백엔드_함수_분석_및_역할_분석_명세서.md`
- 기준 코드 범위: `app/api/*`, `app/core/config.py`, `app/main.py`, `app/models/*`
- API 베이스 경로: 기본 ` /api ` (`settings.API_V1_STR`)
- 문서 목적:
  - 프론트엔드/모바일/운영도구가 사용할 HTTP API 계약을 정리
  - 라우터별 응답 형식 차이(래퍼형/직반환형/리다이렉트/SSE)를 명확히 구분
  - 리팩토링 이후에도 유지해야 하는 외부 계약을 한 문서에서 확인 가능하게 함

## 목차

1. [문서 사용법 / 범위](#문서-사용법--범위)
2. [전체 API 구조 요약](#전체-api-구조-요약)
3. [공통 규약 (인증, 응답, 에러, 미디어 타입)](#공통-규약-인증-응답-에러-미디어-타입)
4. [핵심 도메인 개념/데이터 모델 요약](#핵심-도메인-개념데이터-모델-요약)
5. [라우터/엔드포인트 전체 인벤토리](#라우터엔드포인트-전체-인벤토리)
6. [도메인별 상세 API 명세](#도메인별-상세-api-명세)
7. [외부 서비스 연동 계약 (Server A / LLM / Redis)](#외부-서비스-연동-계약-server-a--llm--redis)
8. [호환성/보안/운영 주의사항](#호환성보안운영-주의사항)
9. [테스트/검증 체크리스트 (API 관점)](#테스트검증-체크리스트-api-관점)
10. [변경 이력 / 후속 과제](#변경-이력--후속-과제)

## 문서 사용법 / 범위

- 이 문서는 **외부 계약(HTTP path/method/주요 응답 키/인증 방식)** 중심의 API 명세서다.
- 내부 구현 세부(함수 단위 호출체인/라인번호/분석/개선 우선순위)는 `백엔드_함수_분석_및_역할_분석_명세서.md`를 우선 참조한다.
- 본 문서는 다음 독자를 대상으로 한다.
  - 프론트엔드 개발자: 어떤 경로에 어떤 body를 보내고 무엇을 받는지
  - QA: 정상/실패/fallback 시 응답 패턴 검증 기준
  - 운영/DevOps: 외부 의존성 상태/헬스체크/환경 플래그 영향 파악
  - 신규 백엔드 개발자: 라우터별 공개 계약과 내부 side effect를 빠르게 파악

### 범위 포함

- `app/main.py`에서 `include_router`된 전체 API 라우터
- 앱 레벨 직접 정의 엔드포인트(`/`, `/api/health`, `/api/system/health`, `/api/system/status`, `/api/test`)
- 인증/쿠키/CORS/환경 플래그 등 API 사용성에 직접 영향 주는 설정
- 주요 ORM 모델 필드(응답 이해를 위한 수준)

### 범위 제외

- 내부 서비스 함수의 상세 알고리즘 (`app/services/*` 함수 단위 분석은 정밀 분석 문서 참조)
- DB 마이그레이션 설계/스키마 변경안 (1차 리팩토링 범위 밖)
- 프론트 화면 UI/UX 상세 스펙 (별도 문서 권장)
- 운영 환경별 Reverse Proxy/Nginx 세부 설정 (이 문서에서는 URL 계약 수준만 다룸)

## 전체 API 구조 요약

### 라우터 구조 (현재 코드 기준)

`app/main.py` 기준 라우터 prefix:

- `/api/auth` : Google OAuth + Guest 로그인, 사용자 인증 조회/로그아웃
- `/api/users` : 사용자 설정(TTS 옵션 등)
- `/api/generate` : 비동기 생성 작업(mock 3D job) 생성/상태조회
- `/api/story` : 스토리 생성(시나리오 저장 포함)
- `/api/evaluation` : 대화 평가/요약 저장
- `/api` : 채팅, TTS, Voices, Characters (다수 라우터가 직접 붙음)
- `/api/ai` : AI 보조 생성 API(스토리/캐릭터 상세 생성)
- `/api/system` : 상세 헬스체크
- `/api/model-make` : 음성 학습 파이프라인(upload/start/abort/register)
- `/api` (legacy alias) : `ai.router` 일부 경로를 구형 클라이언트 호환용으로 재노출

### 엔드포인트 수 요약

- 라우터 데코레이터 엔드포인트: **54개** (`app/api/*` 기준)
- 앱 레벨 직접 엔드포인트: **5개** (`/`, `/api/system/health`, `/api/health`, `/api/system/status`, `/api/test`)
- 총 공개 HTTP 엔드포인트(현재 코드상): **59개**
  - 주의: `@app.get` 2개가 하나의 `health()` 함수에 중복 매핑됨 (`/api/system/health`, `/api/health`)

### API 스타일(혼합형) 요약

이 백엔드는 도메인별로 응답 형식이 다소 다르다. 프론트/운영도구는 이를 알고 사용해야 한다.

- `success/data` 래퍼형 (예: `/api/chat`, `/api/tts`, `/api/users/me/settings`, `/api/model-make/*` 일부)
- Pydantic 직접 반환형 (예: `/api/voices`, `/api/voices/{voice_id}`, `/api/voices/link-options`)
- Redirect + 쿠키형 (예: `/api/auth/google/callback`, `/api/auth/logout`)
- SSE 스트리밍형 (예: `/api/chat/stream`)
- 바이너리 응답형 (예: `/api/tts` with `return_binary=true`)
- 외부 API 프록시 raw JSON 중계형 (예: 일부 `voices/train/*`, `model-make/status|log`)

## 공통 규약 (인증, 응답, 에러, 미디어 타입)

### 1) 인증 방식

#### 기본 인증 메커니즘

- 인증 토큰 저장 방식: **HttpOnly Cookie**
  - 사용자(Google): 기본 쿠키명 `access_token` (`AUTH_COOKIE_NAME`)
  - 게스트(서버 guest 세션): 기본 쿠키명 `guest_access_token` (`GUEST_AUTH_COOKIE_NAME`)
- 하위 호환: `Authorization: Bearer <token>` 헤더도 지원
- 인증 진입 의존성: `app/api/deps.py::get_current_user`
- 선택적 인증 의존성: `get_current_user_optional` (`/chat`, `/chat/stream` 등 비로그인 허용 경로)
- 사용자/게스트 통합 해석 의존성: `get_current_principal_optional` (user 우선, guest fallback)

#### Google OAuth 흐름

1. `GET /api/auth/google/login` -> Google SSO 리다이렉트
2. `GET /api/auth/google/callback` -> 사용자 검증/생성 -> JWT 발급 -> 프론트 콜백으로 Redirect + Cookie 설정
3. 이후 인증 필요 경로에서 쿠키/헤더 토큰 사용

#### Guest 로그인 흐름 (백엔드 guest 세션 + 프론트 탭세션 병행)

1. `POST /api/auth/guest/login` -> 서버 `guest_sessions` row 생성 + guest JWT 쿠키(`guest_access_token`) 발급
2. 프론트는 기존 탭 세션(`sessionStorage`) guest 상태와 함께 사용
3. `GET /api/auth/me`는 guest 쿠키가 있으면 guest principal 응답 (`auth_mode="guest"`)
4. `POST /api/auth/logout`는 user/guest 쿠키 모두 제거 시도 + guest logout 이벤트 기록

#### 개발 편의 fallback (보안 플래그)

- `DEV_AUTH_FALLBACK_ENABLED=true`이면 토큰 검증 실패/미존재 시 `dev-user` 폴백 가능
- 단, guest 쿠키가 있는 요청은 user-only dependency에서 `dev-user` 폴백으로 내려가지 않도록 차단됨 (관리자 경로 보호 목적)
- 운영 환경에서는 `false` 권장
- 이 동작은 보안 리스크이므로 startup 경고 로그 대상

### 2) 권한 모델 (역할 수준)

- 비인증 접근 가능: 일부 공개 경로 (`/chat`, `/tts`, `/voices`, `legacy ai` 일부, 헬스체크 등)
- 인증 필요: 사용자 설정, model-make, 일부 character/AI 생성 경로 등
- 관리자 필요 (`require_admin`): Server A 파일 관리, Voice CRUD/학습 프록시 등 운영성 강한 경로
- 관리자 판정 기준: `ADMIN_EMAILS` 환경변수에 포함된 이메일
- 프론트 `/admin/voices`는 UX 차단용 페이지 가드를 추가했지만, **최종 권한 판정은 백엔드 `require_admin`** 이 담당

### 3) 공통 응답 패턴

#### A. 래퍼형 (가장 흔함)

```json
{
  "success": true,
  "data": { ... }
}
```

변형:

```json
{
  "success": true,
  "message": "..."
}
```

#### B. FastAPI `HTTPException` 에러형 (표준)

```json
{
  "detail": "에러 메시지"
}
```

주의:
- 도메인마다 `detail` 문자열 prefix 정책이 다름 (예: `TTS Error: ...`, `학습 시작 실패: ...`)
- 1차 리팩토링에서는 **메시지 형식 호환 유지**가 우선이라 세부 문구 표준화가 완전하지 않음

#### C. Pydantic 직접 반환형 (voices 계열 일부)

예: `GET /api/voices`

```json
{
  "voices": [ ... ],
  "total": 12
}
```

예: `GET /api/voices/{voice_id}`

```json
{
  "id": "...",
  "name": "...",
  ...
}
```

#### D. SSE 스트리밍형 (`/api/chat/stream`)

- `Content-Type: text/event-stream`
- 각 프레임은 `data: <json>\n\n` 형태
- 청크 프레임/완료 프레임/에러 프레임의 shape가 다름 (아래 `/chat/stream` 상세 참조)

#### E. 바이너리 응답형 (`/api/tts`)

- 요청 body의 `return_binary=true`일 때 오디오 바이너리 반환
- `Content-Disposition` 헤더 포함
- `media_type` 필드에 따라 `audio/wav`, `audio/ogg`, `audio/aac`, `audio/raw`

### 4) 컨텐츠 타입/전송 규약

- 기본 JSON 요청/응답: `application/json`
- 파일 업로드: `multipart/form-data`
  - `generate` router (`/api/generate/`) image 업로드
  - voice train file 업로드 (`/api/voices/server-files/upload`)
  - model-make upload (`/api/model-make/upload`)
- SSE: `text/event-stream`
- 바이너리 오디오: `audio/*`

### 5) CORS / 쿠키 관련 주의

- CORS origin은 `BACKEND_CORS_ORIGINS` 기반으로 설정
- `allow_credentials=True` 활성화
- Cookie 옵션:
  - `AUTH_COOKIE_SECURE` (기본 `false`, 운영 HTTPS에서는 `true` 권장)
  - `AUTH_COOKIE_SAMESITE` (기본 `lax`)
- 프론트에서 쿠키 인증 사용 시 `credentials: include` 필요

## 핵심 도메인 개념/데이터 모델 요약

### 사용자 (`User`)

역할:
- Google OAuth 기반 사용자 계정
- 캐릭터/시나리오/음성/설정의 소유자

핵심 필드:
- `id` (UUID 문자열)
- `email` (unique)
- `username`, `picture`
- `provider` (`google` 중심)
- `created_at`, `updated_at`

### 게스트 세션 (`GuestSession`)

역할:
- 서버가 발급한 guest 인증 쿠키와 매핑되는 세션 엔티티
- guest 로그인/로그아웃/만료 상태 관리

핵심 필드:
- `id` (guest session UUID)
- `display_name`
- `status` (`active`, `logged_out`, `expired`)
- `created_at`, `last_seen_at`, `ended_at`, `expires_at`
- `client_ip`, `user_agent`

### 인증 감사 이벤트 (`AuthEvent`)

역할:
- Google/Guest 로그인/로그아웃/인증 실패를 서버 DB에 남기는 감사 로그

핵심 필드:
- `actor_type`, `actor_id`
- `event_type` (`login`, `logout`, `auth_me`, `token_invalid`, `expired`)
- `provider` (`google`, `guest`)
- `success`, `reason`
- `client_ip`, `user_agent`, `created_at`

### 캐릭터 (`Character`)

역할:
- 대화용 페르소나 저장
- DB 캐릭터 + preset JSON 기반 캐릭터가 혼재

핵심 필드:
- `id`, `name`, `description`, `persona`
- `voice_id` (연결된 Voice)
- `category`, `tags` (JSON 문자열 저장)
- `image_url`, `model_url`, `thumbnail_url`
- `is_preset`, `user_id`

### 음성 (`Voice`)

역할:
- GPT-SoVITS 참조 오디오 및 가중치 경로 메타데이터 저장

핵심 필드:
- `ref_audio_path`, `prompt_text`, `prompt_lang`
- `gpt_weights_path`, `sovits_weights_path`, `model_version`
- `train_input_dir`, `training_model_name` (model-make 정리/삭제 흐름 연결)
- `is_default`, `is_active`, `user_id`

### 오디오 파일 메타 (`AudioFile`)

역할:
- TTS 생성 결과 메타(파일 경로/URL/포맷/길이/캐시 키) 저장

핵심 필드:
- `file_path`, `file_url`, `file_size`, `duration`, `format`
- `voice_id`, `text_hash`
- 로그인 사용자 TTS 캐시에 사용

### 채팅 메시지 (`ChatMessage`) / 요약 (`ChatSummary`) / 시나리오 (`Scenario`)

- `ChatMessage`: session_id 단위 대화 로그 (user/assistant)
- `ChatSummary`: session_id별 최신 요약 1개 (업서트/갱신 성격)
- `Scenario`: 대화 시작 전 배경/요약 스냅샷 저장

### 사용자 설정 (`UserPreference`)

- `user_id` PK + `settings(JSON)`
- 현재 주로 TTS 관련 사용자 설정 보관
- 허용 키는 라우터에서 제한

## 라우터/엔드포인트 전체 인벤토리

### 라우터 엔드포인트 (54개)

| Tag/Router | Method | Full Path | Auth | 응답 스타일 | 주요 용도 |
|---|---|---|---|---|---|
| auth | GET | `/api/auth/google/login` | 공개 | Redirect | Google 로그인 시작 |
| auth | GET | `/api/auth/google/callback` | 공개(SSO 콜백) | Redirect+Cookie | Google 로그인 완료 |
| auth | POST | `/api/auth/guest/login` | 공개 | JSON 객체 + Set-Cookie | Guest 로그인 시작(서버 guest 세션 생성) |
| auth | GET | `/api/auth/me` | 인증(google/guest) | JSON 객체 | 현재 인증 주체(user/guest) 조회 |
| auth | POST | `/api/auth/logout` | 공개/쿠키기반 | Redirect | 쿠키 삭제 로그아웃 |
| users | GET | `/api/users/me/settings` | 인증 | `success/data` | 사용자 설정 조회 |
| users | PUT | `/api/users/me/settings` | 인증 | `success/data` | 사용자 설정 부분 갱신 |
| generate | POST | `/api/generate/` | 공개 | `success/data` | mock 비동기 생성 job 생성 |
| generate | GET | `/api/generate/status/{job_id}` | 공개 | `success/data` | 생성 job 상태 조회 |
| story | POST | `/api/story/generate/story` | 인증 | `success/data` (fallback 가능) | 시나리오 생성 + 저장 |
| evaluation | POST | `/api/evaluation/evaluate` | (코드상 공개) | `success/data` (fallback 가능) | 대화 평가 + 요약 저장 |
| chat | POST | `/api/chat` | 선택적 인증 | `success/data` | 일반/감독 모드 채팅 1턴 |
| chat | POST | `/api/chat/stream` | 선택적 인증 | SSE | 스트리밍 채팅 |
| tts | POST | `/api/tts/prepare` | 공개 | `success/message` | TTS 가중치 선로딩 |
| tts | POST | `/api/tts` | 공개 | JSON or Binary | TTS 합성 |
| tts | GET | `/api/tts/voices` | 공개 | `success/data` | TTS용 음성 목록(간이) |
| voices | GET | `/api/voices/server-files` | 관리자 | raw JSON proxy | Server A 파일 인덱스 |
| voices | POST | `/api/voices/server-files/upload` | 관리자 | `success/data` | train_voice 파일 업로드 |
| voices | DELETE | `/api/voices/server-files` | 관리자 | raw JSON/wrapper | Server A 파일 삭제 |
| voices | POST | `/api/voices/server-files/mkdir` | 관리자 | raw JSON/wrapper | Server A 폴더 생성 |
| voices | POST | `/api/voices/server-files/trim` | 관리자 | raw JSON/wrapper | 오디오 trim/prepare |
| voices | GET | `/api/voices/link-options` | 인증 | `List[VoiceLinkOption]` | 캐릭터-voice 연결 옵션 |
| voices | GET | `/api/voices` | 공개 | `VoiceListResponse` | Voice 목록 조회 |
| voices | GET | `/api/voices/{voice_id}` | 공개 | `VoiceResponse` | Voice 상세 조회 |
| voices | POST | `/api/voices` | 관리자 | `VoiceResponse` | Voice 생성 |
| voices | PUT | `/api/voices/{voice_id}` | 관리자 | `VoiceResponse` | Voice 수정 |
| voices | DELETE | `/api/voices/{voice_id}` | 관리자 | `success/message` | Voice 비활성화/삭제 |
| voices | POST | `/api/voices/{voice_id}/test` | 공개 | `success/data` | TTS 미리듣기(base64) |
| voices | POST | `/api/voices/train/start` | 관리자 | raw JSON proxy | 학습 시작 프록시 |
| voices | GET | `/api/voices/train/status/{model_name}` | 관리자 | raw JSON proxy | 학습 상태 프록시 |
| voices | GET | `/api/voices/train/log/{model_name}` | 관리자 | raw JSON proxy | 학습 로그 프록시 |
| ai | POST | `/api/ai/generate/story` | 공개 | `success/data` (fallback 가능) | AI 스토리 생성 |
| ai | POST | `/api/ai/story/analyze` | 공개 | alias | 구형 story analyze (ai prefix) |
| ai | POST | `/api/ai/generate/character-details` | 인증 | `success/is_fallback/data` | 캐릭터 상세 생성 |
| characters | GET | `/api/characters/presets` | 공개 | `success/data` | preset 캐릭터 목록 |
| characters | GET | `/api/characters` | 공개(개발용) | `success/data` | dev-user 캐릭터 목록 |
| characters | POST | `/api/characters` | 공개(개발용) | `success/data` | dev-user 캐릭터 생성 |
| characters | GET | `/api/characters/my` | 인증 | `success/data` | 내 캐릭터 목록 |
| characters | POST | `/api/characters/my` | 인증 | `success/data` | 내 캐릭터 생성 |
| characters | GET | `/api/characters/admin/all` | 관리자 | `success/data` | DB+preset 통합 목록 |
| characters | PATCH | `/api/characters/admin/{character_id}/voice` | 관리자 | `success/data` | 관리자 voice 연결/해제 |
| characters | GET | `/api/characters/{character_id}` | 공개 | `success/data` | 캐릭터 상세 (preset 우선) |
| characters | PUT | `/api/characters/{character_id}` | 인증 | `success/data` | 캐릭터 수정 |
| characters | DELETE | `/api/characters/{character_id}` | 인증 | `success/data` | 캐릭터 삭제 |
| characters | POST | `/api/generate` | 인증 | `success/data` (fallback 가능) | 캐릭터 페르소나 초안 생성 |
| system | GET | `/api/system/health/detailed` | 공개 | `success/services` | DB/외부 서비스 상세 헬스 |
| model-make | POST | `/api/model-make/upload` | 인증 | `success/data` | 훈련용 WAV 업로드 |
| model-make | POST | `/api/model-make/start` | 인증 | raw JSON proxy | 학습 시작 |
| model-make | POST | `/api/model-make/abort` | 인증 | `success/message` | 모델 제작 중단/정리 |
| model-make | GET | `/api/model-make/status/{model_name}` | 인증 | raw JSON proxy | 학습 상태 조회 |
| model-make | GET | `/api/model-make/log/{model_name}` | 인증 | raw JSON proxy | 학습 로그 조회 |
| model-make | POST | `/api/model-make/register` | 인증 | `success + voice_id...` | 학습 결과 Voice 등록 |
| model-make | GET | `/api/model-make/my` | 인증 | `success/data` | 내 모델 제작 음성 목록 |
| model-make | DELETE | `/api/model-make/my/{voice_id}` | 인증 | `success/message` | 내 모델 제작 음성 + Server A 자원 삭제 |
| legacy(ai alias) | POST | `/api/generate/story` | 공개 | `success/data` (fallback 가능) | 구형 스토리 생성 alias |
| legacy(ai alias) | POST | `/api/story/analyze` | 공개 | alias | 구형 분석 alias |
| legacy(ai alias) | POST | `/api/generate/character-details` | 인증 | `success/is_fallback/data` | 구형 캐릭터 상세 생성 alias |

### 앱 레벨 직접 엔드포인트 (5개)

| Method | Full Path | 응답 스타일 | 용도 |
|---|---|---|---|
| GET | `/` | `success/message` | 루트 생존 확인 |
| GET | `/api/system/health` | `success/data.status` | 간단 헬스 |
| GET | `/api/health` | `success/data.status` | 레거시/직접 헬스 |
| GET | `/api/system/status` | `success/data` | 상태/버전/GPU 서버 상태 표시용 |
| GET | `/api/test` | JSON 객체 | 프론트 연결 테스트/CORS 확인 |

## 도메인별 상세 API 명세

> 표기 규칙
> - `Auth`: `공개`, `선택적`, `인증`, `관리자`
> - `성공 응답`: 대표 shape 위주로 기재 (실제 부가 필드는 도메인별로 다를 수 있음)
> - `부작용`: DB/파일/외부 HTTP/Redis 기준
> - `fallback`: 장애 시 성공 shape 유지 정책 여부

### 1. Auth API (`/api/auth/*`)

#### 1-1. `GET /api/auth/google/login`

- Auth: 공개
- 역할: Google OAuth 로그인 시작 (리다이렉트)
- 요청: 없음
- 성공 응답: Google 로그인 페이지로 Redirect
- 부작용: 없음 (서버 상태 변경 없음)
- 주의:
  - 실제 redirect URI는 `GOOGLE_REDIRECT_URI` / `BACKEND_PUBLIC_URL` 설정에 영향 받음
  - `GOOGLE_SSO_ALLOW_INSECURE_HTTP`가 `true`면 로컬 HTTP 환경 허용

#### 1-2. `GET /api/auth/google/callback`

- Auth: 공개 (Google이 호출하는 콜백)
- 역할: OAuth 콜백 검증 -> 사용자 조회/생성 -> JWT 발급 -> 프론트 리다이렉트 + HttpOnly Cookie 설정
- 요청 파라미터: Google OAuth provider callback query 파라미터
- 성공 응답: `RedirectResponse` (`<FRONTEND_URL>/auth/callback`)
- 쿠키 설정:
  - 쿠키명: `AUTH_COOKIE_NAME` (기본 `access_token`)
  - `HttpOnly=true`
  - `secure=AUTH_COOKIE_SECURE`
  - `samesite=AUTH_COOKIE_SAMESITE`
- 부작용:
  - DB `users` 조회/생성
  - JWT 생성
  - Set-Cookie 헤더
- 실패 응답:
  - `408` Google 인증 타임아웃
  - `400` Google 인증 실패/유저정보 취득 실패
  - `500` DB 또는 JWT 생성 실패

#### 1-3. `POST /api/auth/guest/login`

- Auth: 공개
- 역할: 서버 guest 세션 생성 + guest JWT 쿠키 발급 + 인증 감사 로그 기록
- 요청 body (선택):

```json
{ "display_name": "게스트" }
```

- 성공 응답 (대표):

```json
{
  "id": "guest-session-uuid",
  "email": null,
  "username": "게스트",
  "picture": null,
  "provider": "guest",
  "is_admin": false,
  "auth_mode": "guest",
  "is_guest": true
}
```

- 쿠키 설정:
  - 쿠키명: `GUEST_AUTH_COOKIE_NAME` (기본 `guest_access_token`)
  - `HttpOnly=true`
  - `secure=AUTH_COOKIE_SECURE`
  - `samesite=AUTH_COOKIE_SAMESITE`
  - `GUEST_COOKIE_SESSION_ONLY=true`이면 브라우저 세션 쿠키로 발급 (`max_age` 미설정)
- 부작용:
  - DB `guest_sessions` INSERT
  - DB `auth_events(login)` INSERT

#### 1-4. `GET /api/auth/me`

- Auth: 인증 (Google 또는 Guest 쿠키)
- 역할: 현재 인증 주체(user/guest) 정보 반환 (프론트 초기 부팅 시 자주 호출)
- 성공 응답 (대표):

```json
{
  "id": "uuid",
  "email": "user@example.com",
  "username": "홍길동",
  "picture": "https://...",
  "provider": "google",
  "is_admin": false,
  "auth_mode": "google",
  "is_guest": false
}
```

- guest 응답 예시:

```json
{
  "id": "guest-session-uuid",
  "email": null,
  "username": "게스트",
  "picture": null,
  "provider": "guest",
  "is_admin": false,
  "auth_mode": "guest",
  "is_guest": true
}
```

- 부작용:
  - user: DB 조회
  - guest: `guest_sessions.last_seen_at` 갱신(베스트 에포트)
- 주의:
  - `is_admin`은 `ADMIN_EMAILS` 설정 기반 계산값
  - user/guest 쿠키가 동시에 있으면 user(Google) 우선 판정

#### 1-5. `POST /api/auth/logout`

- Auth: 사실상 쿠키 보유 클라이언트 대상 (의존성 강제 없음)
- 역할: 프론트 루트로 Redirect + user/guest auth cookie 삭제
- 성공 응답: Redirect (`<FRONTEND_URL>/`)
- 부작용:
  - `delete_cookie` (user/guest 둘 다 시도)
  - guest/user logout `auth_events` 기록
  - guest 로그아웃일 경우 `guest_sessions.status=logged_out`, `ended_at` 갱신
- 주의: 삭제 옵션(path/samesite/secure)은 설정과 일치해야 브라우저가 정상 삭제

### 2. User Settings API (`/api/users/*`)

#### 2-1. `GET /api/users/me/settings`

- Auth: 인증
- 역할: 로그인 사용자의 설정 JSON 조회
- 성공 응답:

```json
{ "success": true, "data": { "tts_enabled": true, "tts_speed": 1.0 } }
```

- 데이터가 없으면 `{ "success": true, "data": {} }`
- 부작용: 없음 (DB 조회)

#### 2-2. `PUT /api/users/me/settings`

- Auth: 인증
- 역할: 사용자 설정 부분 업데이트(merge)
- 요청 body: `Dict[str, Any]` (허용 키만 반영)
- 허용 키:
  - `tts_mode`
  - `tts_delay_ms`
  - `tts_streaming_mode`
  - `tts_enabled`
  - `tts_speed`
- 성공 응답: 갱신 후 전체 settings (`success/data`)
- 부작용:
  - DB `user_preferences` upsert/update
- 주의:
  - 허용되지 않은 키는 무시됨
  - 유효 키가 하나도 없으면 기존값 조회 후 반환

### 3. Generate API (비동기 생성 Job, `/api/generate/*`)

#### 3-1. `POST /api/generate/`

- Auth: 공개 (현재 코드 기준)
- 역할: 업로드 이미지 기반 mock 3D 생성 job 생성
- 요청 형식: `multipart/form-data`
  - `image` (필수 파일)
- 성공 응답:

```json
{
  "success": true,
  "data": {
    "job_id": "uuid",
    "status_url": "/api/generate/status/{job_id}"
  }
}
```

- 부작용:
  - 로컬 `./uploads`에 입력 이미지 저장
  - DB `generation_jobs` 생성
  - Background task 등록 (mock 처리)
- 주의:
  - 현재는 mock 구현 성격이 강함 (`USER_ASSETS_DIR`에 mock `.glb` 생성)
  - 인증/소유권 제한이 강하지 않음 (1차 범위 기준)

#### 3-2. `GET /api/generate/status/{job_id}`

- Auth: 공개 (현재 코드 기준)
- 역할: 생성 job 상태/진행률/결과 URL 조회
- 성공 응답:

```json
{
  "success": true,
  "data": {
    "job_id": "...",
    "status": "pending|processing|completed|failed",
    "progress": 0,
    "result_url": "/assets/models/...glb",
    "error": null
  }
}
```

- 실패 응답: `404 Job not found`
- 부작용: 없음 (DB 조회)

### 4. Story / Evaluation API

#### 4-1. `POST /api/story/generate/story`

- Auth: 인증
- 역할: 상황 키워드로 UI용 `summary` + AI 프롬프트용 `background` 생성 후 `Scenario` 저장
- 요청 body (`StoryGenerationRequest`):
  - `user_name: str`
  - `character_name: str`
  - `situation: str`
- 성공 응답 (정상/실패 fallback 모두 `success=true` 유지):

```json
{
  "success": true,
  "data": {
    "scenario_id": "uuid",
    "summary": "...",
    "background": "..."
  }
}
```

- fallback 정책:
  - LLM/JSON 파싱/DB 저장 실패 시에도 기본 summary/background를 반환 (저장은 누락될 수 있음)
- 부작용:
  - 외부 LLM 호출
  - DB `scenarios` insert (정상 시)

#### 4-2. `POST /api/evaluation/evaluate`

- Auth: 코드상 인증 강제 없음 (요청 body 기반 평가)
- 역할: 대화 평가 JSON 생성 + `context_manager`를 통한 세션 요약 갱신 저장
- 요청 body (`EvaluationRequest`):
  - `messages: List[{role, content}]`
  - `character_name`
  - `character_persona`
  - `user_name` (optional)
  - `session_id` (optional)
- 성공 응답 (대표):

```json
{
  "success": true,
  "data": {
    "summary": "...",
    "score": 85,
    "feedback": "..."
  }
}
```

- fallback 정책:
  - LLM/파싱/요약 저장 실패 시 `score=50`, 기본 feedback/summary 반환 (`success=true` 유지)
- 부작용:
  - 외부 LLM 호출
  - `context_manager`를 통한 요약 조회/생성/DB 저장

### 5. Chat API (`/api/chat`, `/api/chat/stream`)

#### 공통 개념 (ChatRequest)

`ChatRequest` 주요 필드:

- `messages: List[{role, content}]` (필수)
- `persona: Optional[str]`
- `temperature`, `max_tokens`, `model`
- `session_id` (없으면 서버 생성)
- `character_id`, `scenario`, `director_note`, `current_speaker`
- TTS 관련:
  - `tts_enabled`
  - `tts_mode`
  - `tts_delay_ms`
  - `tts_streaming_mode`
  - `tts_speed`

역할 분기:

- 일반(배우) 모드: 캐릭터 1명으로 응답
- 감독 모드: 페르소나 포맷을 해석해 2캐릭터 교대 응답

#### 5-1. `POST /api/chat`

- Auth: 선택적 (`get_current_user_optional`)
- 역할: 한 턴 응답 생성 + DB 저장 + (옵션) TTS `audio_url` 첨부
- 성공 응답(정상):

```json
{
  "success": true,
  "data": {
    "content": "...",
    "usage": { "prompt_tokens": 0, "completion_tokens": 0 },
    "session_id": "uuid",
    "context_summarized": false,
    "audio_url": "/assets/...wav"
  }
}
```

- `audio_url`는 TTS 성공 시에만 포함
- fallback 정책 (중요):
  - LLM/내부 오류 발생 시에도 `success=true` mock 응답 반환 (개발/UX 호환 계약)
- 부작용:
  - `context_manager` 요약/컨텍스트 경로 가능
  - 외부 LLM 호출 (`Gemini` 또는 `Ollama`)
  - DB `chat_messages` 저장 (user/assistant)
  - 내부 TTS 호출 + 파일 저장 + 오디오 메타 저장(로그인 시 캐시)

#### 5-2. `POST /api/chat/stream`

- Auth: 선택적
- 역할: SSE 스트리밍으로 응답 청크 전송
- 응답 헤더:
  - `Content-Type: text/event-stream`
  - `Cache-Control: no-cache`
  - `X-Accel-Buffering: no`
- SSE 프레임 규약:
  - 청크 프레임:
    ```json
    { "content": "청크", "done": false }
    ```
  - 완료 프레임:
    ```json
    { "content": "", "done": true, "full_content": "전체응답", "session_id": "..." }
    ```
  - 에러 프레임:
    ```json
    { "error": "...", "done": true }
    ```
- 주의:
  - `/chat`와 달리 현재 정책상 DB 저장/TTS 첨부보다 스트리밍 호환성이 우선
  - 프론트는 `done`, `full_content`, `session_id` 키에 의존

### 6. TTS API (`/api/tts*`)

#### 6-1. `POST /api/tts/prepare`

- Auth: 공개
- 역할: 채팅 시작 전 특정 voice의 GPT/SoVITS 가중치 선로딩 시도
- 요청 body: `{ "voice_id": "..." }`
- 성공 응답 예시:
  - 정상 로드:
    ```json
    { "success": true, "message": "Weights prepared for <voice_id>" }
    ```
  - legacy voice (JSON config) 경유:
    ```json
    { "success": true, "message": "Legacy voice config used (no weights path)" }
    ```
  - voice 미발견(채팅 진행은 허용):
    ```json
    { "success": false, "message": "Voice not found, skipped" }
    ```
- 부작용: Server A set_weights 호출(최대 2회)

#### 6-2. `POST /api/tts`

- Auth: 공개
- 요청 body: `TTSRequest` (필드 많음)
- 핵심 필드:
  - `text` (필수)
  - `voice_id` (선택, DB→legacy JSON fallback)
  - `ref_audio_path` (선택; voice_id 없을 때 직접 지정 가능)
  - `media_type`, `streaming_mode`, `return_binary`
  - GPT-SoVITS 생성 파라미터 (`top_k`, `temperature`, `speed_factor` 등)
- 성공 응답 분기:

A) `return_binary=false` (기본):

```json
{
  "success": true,
  "data": {
    "audio_base64": "...",
    "format": "wav",
    "voice_id": "default"
  }
}
```

B) `return_binary=true`:
- 오디오 바이너리 본문 반환 (`audio/wav` 등)
- `Content-Disposition: attachment; filename=tts_output.<format>`

- 실패 정책:
  - `ValueError` -> `400` (`TTS Error: ...`)
  - 기타 내부 오류 -> `500` (`TTS Error: ...`)
- 부작용:
  - 외부 TTS 호출(Server A)
  - 공개 라우터 자체는 DB 캐시 저장 안 함 (`_synthesize_tts_internal` 경로와 다름)

#### 6-3. `GET /api/tts/voices`

- Auth: 공개
- 역할: 활성화된 음성의 간이 목록 + 기본 voice_id 제공
- 성공 응답:

```json
{
  "success": true,
  "data": {
    "voices": [{"id":"...","name":"...","language":"ko","description":""}],
    "default_voice_id": "..."
  }
}
```

### 7. Voices API (`/api/voices*`)

#### 7-1. Server A 파일 관리 (관리자)

##### `GET /api/voices/server-files`

- Auth: 관리자
- 역할: Server A 파일 인덱스 조회 (`models/train_voices/logs`)
- 성공 응답:
  - 가능하면 `/api/files/all` 응답을 그대로 중계
  - 미지원 시 `models`, `train_voices`, `logs` 개별 조회 결과 조합
- 주의: 반환 키 구조는 프론트 파일 매니저와 직접 연결됨

##### `POST /api/voices/server-files/upload`

- Auth: 관리자
- 형식: `multipart/form-data`
- 입력: `file`, `sub_path`
- 역할: `sample_train_voice` 하위로 오디오 업로드
- 검증: 확장자 제한 (`wav/mp3/flac/ogg`)
- 부작용: Server A 파일 업로드

##### `DELETE /api/voices/server-files`

- Auth: 관리자
- 역할: Server A 파일/폴더 삭제 프록시
- 요청: path 기반 삭제 파라미터(구현 기준)
- 주의: 실제 body/query는 프론트 파일 매니저와 맞춰 사용

##### `POST /api/voices/server-files/mkdir`

- Auth: 관리자
- 역할: Server A 폴더 생성 프록시

##### `POST /api/voices/server-files/trim`

- Auth: 관리자
- 역할: 오디오 trim/prepare 계열 Server A API 프록시

#### 7-2. Voice 조회/관리

##### `GET /api/voices/link-options`

- Auth: 인증
- 역할: 캐릭터 `voice_id` 선택 UI용 옵션 목록
- 성공 응답: `List[VoiceLinkOption]`
- 포함 정책: `user_id == me` 또는 시스템 음성(`user_id is null`), `is_active == true`

##### `GET /api/voices`

- Auth: 공개
- 쿼리: `active_only=true|false` (기본 `true`)
- 성공 응답: `VoiceListResponse`
  - `voices: VoiceResponse[]`
  - `total: int`

##### `GET /api/voices/{voice_id}`

- Auth: 공개
- 성공 응답: `VoiceResponse`
- 실패: `404 음성을 찾을 수 없습니다`

##### `POST /api/voices`

- Auth: 관리자
- 요청 body: `VoiceCreateRequest`
- 주요 입력:
  - `name`, `ref_audio_path`, `prompt_*`, `weights_path`, `is_default`, `is_active`
  - `train_input_dir`, `training_model_name` (옵션)
- 처리 특징:
  - `is_default=true`면 기존 기본 음성 해제
  - `ref_audio_path`는 Server A 검증/자동 trim (`process_ref_audio`) 수행
- 성공 응답: `VoiceResponse`
- 부작용: DB `voices` insert, Server A validate/prepare 호출

##### `PUT /api/voices/{voice_id}`

- Auth: 관리자
- 요청 body: `VoiceUpdateRequest` (부분 업데이트)
- 처리 특징:
  - `is_default` 전환 시 기존 기본 음성 해제
  - `ref_audio_path` 변경 시 재검증/자동 trim
- 성공 응답: `VoiceResponse`

##### `DELETE /api/voices/{voice_id}`

- Auth: 관리자
- 쿼리: `permanent=false|true`
- 동작:
  - `permanent=false` -> `is_active=False` (비활성화)
  - `permanent=true` -> DB 완전 삭제
- 성공 응답: `success/message`

##### `POST /api/voices/{voice_id}/test`

- Auth: 공개 (현재 코드 기준)
- 요청 body: `VoiceTestRequest` (`text` 기본값 제공)
- 역할: 지정 voice로 TTS 미리듣기(base64) 생성
- 성공 응답:

```json
{
  "success": true,
  "data": {
    "audio_base64": "...",
    "format": "wav",
    "voice_id": "...",
    "voice_name": "...",
    "text": "안녕하세요..."
  }
}
```

- 부작용: Server A TTS 호출

#### 7-3. Voices 학습 프록시 (`/api/voices/train/*`, 관리자)

- `POST /api/voices/train/start`
  - Auth: 관리자
  - 요청: `TrainStartRequest`
  - 응답: Server A train start raw JSON 중계
- `GET /api/voices/train/status/{model_name}`
  - Auth: 관리자
  - 응답: raw JSON 중계
  - 404는 `allow_404_detail`로 의미 있는 메시지 유지
- `GET /api/voices/train/log/{model_name}`
  - Auth: 관리자
  - 응답: raw JSON 중계
  - 로그 미존재 404 허용 정책 유지

### 8. Model-Make API (`/api/model-make/*`)

이 도메인은 사용자 관점의 음성 모델 제작 파이프라인을 구성한다.

흐름: `upload -> start -> (status/log polling) -> register` 또는 `abort`

#### 8-1. `POST /api/model-make/upload`

- Auth: 인증
- 형식: `multipart/form-data`
- 입력: `files[]` (WAV 3개 이상)
- 검증 규칙:
  - 최소 3개
  - `.wav`만 허용
  - 총합 100MB 이하
  - 각 파일 5초 이상 (`mutagen`)
- 저장/업로드:
  - Server A `sample_train_voice/user_{user_id}/run_{ts}`
- 성공 응답:

```json
{
  "success": true,
  "data": {
    "train_input_dir": "user_<id>/run_<ts>",
    "first_file": "sample1.wav"
  }
}
```

- 부작용: Server A 파일 업로드 (다수 요청)

#### 8-2. `POST /api/model-make/start`

- Auth: 인증
- 요청 body: `ModelMakeStartRequest`
  - `model_name`, `train_input_dir`, `version`
- 처리 특징:
  - `train_input_dir` -> `SERVER_A_TRAIN_VOICE_ROOT/train_input_dir`로 조합 후 Server A 호출
  - 모델 버전 whitelist 검증
- 성공 응답: Server A `/api/train/start` raw JSON 중계
- 실패: `400` (버전 오류), `HTTPException` (Server A 프록시 실패)

#### 8-3. `POST /api/model-make/abort`

- Auth: 인증
- 요청 body: `ModelMakeAbortRequest`
  - `train_input_dir` 필수
  - `model_name` 선택
- 역할: 업로드 음성/train logs/TEMP 삭제 요청 (최대한 정리)
- 실패 정책:
  - 일부 삭제 실패는 흡수하고 계속 진행 (중복 클릭/재시도 UX 허용)
- 성공 응답:

```json
{ "success": true, "message": "중단되었고, 업로드·학습 관련 리소스가 삭제 요청되었습니다." }
```

#### 8-4. `GET /api/model-make/status/{model_name}`

- Auth: 인증
- 역할: Server A 학습 상태 조회 프록시
- 응답: raw JSON 중계
- 404 메시지 고정 (`학습 상태를 찾을 수 없습니다.`)

#### 8-5. `GET /api/model-make/log/{model_name}`

- Auth: 인증
- 역할: Server A 학습 로그 조회 프록시
- 응답: raw JSON 중계
- 404 메시지 고정 (`로그를 찾을 수 없습니다.`)

#### 8-6. `POST /api/model-make/register`

- Auth: 인증
- 요청 body: `ModelMakeRegisterRequest`
  - `model_name`, `voice_name`, `train_input_dir`, `ref_audio_file`
  - `gpt_weights_path`, `sovits_weights_path` (선택)
- 역할:
  - 학습 결과를 `Voice` 엔티티로 등록
  - `prepare-ref-audio` 보정 시도 후 실패해도 원본으로 진행
  - 가중치 미지정 시 Server A logs 인덱스에서 `model_name`으로 자동 추론 시도
- 성공 응답:

```json
{
  "success": true,
  "voice_id": "uuid",
  "name": "voice name",
  "train_input_dir": "...",
  "training_model_name": "..."
}
```

- 부작용: DB `voices` insert, Server A logs 조회/prepare 호출 가능

#### 8-7. `GET /api/model-make/my`

- Auth: 인증
- 역할: 현재 사용자가 등록한 model-make 음성 목록 요약
- 성공 응답: `success/data.voices[] + total`

#### 8-8. `DELETE /api/model-make/my/{voice_id}`

- Auth: 인증
- 역할:
  - 본인 소유 Voice 삭제
  - 연결 캐릭터 `voice_id = null`
  - Server A `train_input_dir`, `logs/{model_name}` 삭제 시도
- 실패 정책:
  - Server A 삭제 실패는 흡수, DB 삭제는 진행
- 성공 응답: `success/message`

### 9. Characters API (`/api/characters*`, `/api/generate`)

이 라우터는 **dev/user/admin/generate** 성격이 혼재되어 있으므로 경로별 권한/정책을 반드시 구분해야 한다.

#### 9-1. Preset/개발용 경로

##### `GET /api/characters/presets`

- Auth: 공개
- 역할: preset JSON 캐릭터 목록 반환
- 성공 응답: `success/data.characters[]`
- 부작용: preset 파일 읽기 (`PRESET_CHARACTERS_DIR`)

##### `GET /api/characters`

- Auth: 공개 (개발용)
- 역할: `dev-user`의 DB 캐릭터 목록 조회
- 주의: 운영 비활성화 후보

##### `POST /api/characters`

- Auth: 공개 (개발용)
- 요청 body: `CharacterCreate`
- 역할: `dev-user` 캐릭터 생성
- 성공 응답: `success/data` (캐릭터 직렬화 결과)

#### 9-2. 사용자 경로 (인증)

##### `GET /api/characters/my`

- Auth: 인증
- 역할: 내 DB 캐릭터 목록 (`is_preset=false`)
- 성공 응답: `success/data.characters[]`

##### `POST /api/characters/my`

- Auth: 인증
- 요청 body: `CharacterCreate`
- 역할: 내 캐릭터 생성
- 성공 응답: `success/data`

##### `GET /api/characters/{character_id}`

- Auth: 공개
- 역할: 캐릭터 상세 조회 (preset 우선 -> DB fallback)
- 성공 응답: `success/data`
- 주의:
  - preset이면 `is_preset=true` 보정 응답

##### `PUT /api/characters/{character_id}`

- Auth: 인증 (본인 소유만)
- 요청 body: `CharacterUpdate`
- 역할: 캐릭터 수정
- 금지:
  - preset 캐릭터 수정 불가 (`403`)
- 성공 응답: `success/data`

##### `DELETE /api/characters/{character_id}`

- Auth: 인증 (본인 소유만)
- 역할: 캐릭터 삭제
- 금지: preset 캐릭터 삭제 불가
- 성공 응답:

```json
{ "success": true, "data": { "message": "캐릭터가 삭제되었습니다" } }
```

#### 9-3. 관리자 경로

##### `GET /api/characters/admin/all`

- Auth: 관리자
- 역할: preset + DB 캐릭터 통합 목록 (voice 연결 관리용)
- 성공 응답: `success/data.characters[]` (`AdminCharacterListItem` shape)

##### `PATCH /api/characters/admin/{character_id}/voice`

- Auth: 관리자
- 요청 body: `{ "voice_id": "..." | null }`
- 역할: preset JSON 또는 DB 캐릭터의 `voice_id` 연결/해제
- 처리 특징:
  - preset이면 파일(JSON) 업데이트
  - DB 캐릭터면 DB update
- 성공 응답: `success/data`

#### 9-4. 캐릭터 생성 보조 (`POST /api/generate`)

- Auth: 인증
- 요청 body: `GenerateRequest` (`name`, `description`, `category`)
- 역할: 캐릭터 페르소나/설명/tags 초안 생성 (Gemini -> Local LLM -> Mock fallback)
- 성공 응답:

```json
{
  "success": true,
  "data": {
    "persona": "...",
    "description": "...",
    "category": "...",
    "tags": ["..."]
  }
}
```

- fallback 정책:
  - LLM 체인 실패 시 mock data 반환 (`success=true` 유지)

### 10. AI API (`/api/ai/*` + legacy alias `/api/*`)

이 라우터는 `story`, `characters` 도메인과 기능 일부가 겹치지만, 구형/보조 경로 호환성 때문에 유지된다.

#### 10-1. `POST /api/ai/generate/story` + `POST /api/generate/story` (legacy alias)

- Auth: 공개
- 요청 body (`StoryGenerationRequest`):
  - `situation`
  - `opponent_name` (optional)
  - `character_persona` (optional)
  - `model` (optional)
- 성공 응답:

```json
{
  "success": true,
  "data": {
    "plot": "...",
    "background": "..."
  }
}
```

- fallback 정책: LLM 장애 시 mock 스토리 반환 (`success=true` 유지)

#### 10-2. `POST /api/ai/story/analyze` + `POST /api/story/analyze` (legacy alias)

- Auth: 공개
- 역할: 구형 분석 경로 호환용 alias (내부적으로 `generate_story` 호출)
- 성공 응답: `generate/story`와 동일 shape

#### 10-3. `POST /api/ai/generate/character-details` + `POST /api/generate/character-details` (legacy alias)

- Auth: 인증
- 요청 body (`CharacterGenerationRequest`):
  - `name` (필수)
  - `category`, `source_work`, `description`, `worldview` (선택/기본값 있음)
- 역할: 상세 캐릭터 설정 16종 생성 (JSON 스키마 강제)
- 성공 응답:

```json
{
  "success": true,
  "is_fallback": false,
  "data": {
    "name": "...",
    "worldview": "...",
    "personality": "...",
    "likes": ["..."],
    "dislikes": ["..."],
    "...": "..."
  }
}
```

- 실패 정책:
  - Gemini/Ollama fallback 체인 모두 실패 -> `500`
  - JSON 파싱 실패 -> `500`
- 주의: 이 엔드포인트는 `/api/generate`(characters 라우터)와 다른 스키마/의도를 가진다.

### 11. System API (`/api/system/*` + app-level health/status)

#### 11-1. `GET /api/system/health/detailed`

- Auth: 공개
- 역할: DB + 외부 서비스(Ollama/TTS/Server A Files/Train) 병렬 헬스체크
- 성공 응답 구조(대표):

```json
{
  "success": true,
  "timestamp": 1700000000.0,
  "services": {
    "database": {"name":"PostgreSQL DB","status":"online","latency":12,"url":"Internal"},
    "ollama": {"name":"Ollama (LLM)","status":"online","latency":45,"message":"Status: 200","url":"..."},
    "tts": {"name":"GPT-SoVITS (TTS)","status":"online","latency":100,"message":"연결됨 (파라미터 필요)","url":"..."},
    "server_a_files": { ... },
    "server_a_train": { ... }
  }
}
```

- 특징:
  - 일부 4xx도 `online`으로 간주 (연결 가능 여부 중심)
  - 5xx는 `online` + 서버 오류 메시지로 구분 가능

#### 11-2. `GET /api/system/health` 및 `GET /api/health`

- Auth: 공개
- 역할: 간단 헬스체크 (legacy 포함)
- 성공 응답:

```json
{ "success": true, "data": { "status": "healthy" } }
```

#### 11-3. `GET /api/system/status`

- Auth: 공개
- 역할: 상태/버전/GPU 연결 상태 문자열 반환 (운영 대시보드용 경량 정보)

#### 11-4. `GET /api/test`

- Auth: 공개
- 역할: 프론트 연결 테스트 + CORS origin 확인

### 12. Root Endpoint

#### `GET /`

- Auth: 공개
- 역할: 서버 루트 생존 확인
- 응답:

```json
{ "success": true, "message": "Avatar Forge Backend Running" }
```

## 외부 서비스 연동 계약 (Server A / LLM / Redis)

### 1) Server A (GPT-SoVITS 및 보조 API)

#### Files API (Server A 파일 스캐너)

- 설정: `SERVER_A_FILES_API_URL`
- 주요 사용처:
  - `/api/voices/server-files*`
  - `/api/model-make/register` 가중치 추론 (`/api/files/logs`)
- 계약 성격:
  - raw JSON 중계 또는 부분 조합
  - 응답 키(`models`, `train_voices`, `logs`)는 프론트가 직접 사용

#### Training API (Server A 학습 API)

- 설정: `SERVER_A_TRAINING_API_URL`
- 주요 사용처:
  - `/api/model-make/start|status|log`
  - `/api/voices/train/*` 프록시
- 계약 성격:
  - 상태/로그 JSON은 원형 중계 비중이 큼
  - 404를 polling 정상 시나리오로 취급하는 경로 존재

#### TTS API (GPT-SoVITS TTS)

- 설정: `TTS_BASE_URL`, `TTS_API_PATH`
- 주요 사용처:
  - `/api/tts`
  - `/api/voices/{voice_id}/test`
  - 채팅 내부 TTS (`_synthesize_tts_internal` via `tts_service`)
- 계약 성격:
  - 바이너리 오디오 본문 반환
  - 비정상 상태코드 -> `HTTPException` 변환 (공통 helper 사용)

### 2) LLM (Gemini / Ollama)

- 공통 호출 진입점: `app/core/llm.py`
- provider 선택 규칙: 모델명 prefix 기반 (`gemini-`이면 Gemini, 아니면 Ollama 경로)
- 사용처:
  - `/api/chat`, `/api/chat/stream`
  - `/api/story/generate/story`
  - `/api/evaluation/evaluate`
  - `/api/ai/generate/*`
  - `/api/characters/generate`
- fallback 정책 (도메인별 차이):
  - 일부 엔드포인트는 실패 시 `success=true` mock 반환
  - 일부는 `500`으로 실패 전파

### 3) Redis (TTS queue/stream + context manager fallback 경로)

- 설정: `REDIS_URL`
- 주요 사용처:
  - `tts_worker.py`: BRPOP 큐 -> XADD 스트림 relay
  - `context_manager`: 요약/컨텍스트 저장소 fallback 경로
  - 앱 startup/shutdown: Redis pool init/close
- 공개 API 관점 영향:
  - `/chat` TTS/컨텍스트 경로의 지연/안정성에 간접 영향
  - 직접 Redis API 엔드포인트는 없음

## 호환성/보안/운영 주의사항

### 1) 호환성 관련 (1차 리팩토링 정책)

- path/method/주요 응답 key는 유지 우선
- legacy alias 유지:
  - `/api/generate/story`
  - `/api/story/analyze`
  - `/api/generate/character-details`
- `/chat` 오류 시 mock fallback 응답 유지 (개발/UX 호환 계약)
- `/characters` dev 경로 유지 (운영 비활성화 후보지만 1차에서 유지)

### 2) 보안 관련 플래그 (API 사용성에 직접 영향)

- `DEV_AUTH_FALLBACK_ENABLED`
  - `true`: 토큰 없이도 `dev-user`로 인증되는 경로가 생길 수 있음
  - 운영에서는 `false` 권장
- `APP_ENV`
  - `production` + `STRICT_STARTUP_VALIDATION=true` + `DEV_AUTH_FALLBACK_ENABLED=true` 조합이면 startup fail-fast
- `GOOGLE_SSO_ALLOW_INSECURE_HTTP`
  - 로컬 개발 편의를 위해 기본 `true`
  - 운영 HTTPS에서 `false` 권장
- `AUTH_COOKIE_SECURE`, `AUTH_COOKIE_SAMESITE`, `GUEST_AUTH_COOKIE_NAME`, `GUEST_COOKIE_SESSION_ONLY`
  - 프론트 로그인/세션 유지 동작에 직접 영향
- `STRICT_STARTUP_VALIDATION`
  - 기본 정책은 warn-only
  - 단, `APP_ENV=production`에서 일부 조합(`DEV_AUTH_FALLBACK_ENABLED=true`)은 fail-fast 적용

### 3) 운영/테스트 시 자주 헷갈리는 포인트

- `/api/voices` 응답은 `success/data`가 아니라 `VoiceListResponse` 직접 반환형
- `/api/chat/stream`은 SSE라 일반 `fetch().json()`으로 처리 불가
- `/api/tts`는 `return_binary`에 따라 응답 타입이 바뀜
- `/api/model-make/*`와 `/api/voices/train/*`는 둘 다 Server A 학습 API와 연결되지만 대상 사용자/권한/UX 의도가 다름
- `/api/story/generate/story`와 `/api/ai/generate/story`는 이름이 비슷하지만 저장/응답 목적이 다름

## 테스트/검증 체크리스트 (API 관점)

### A. 정적/계약 검증

1. 라우터 인벤토리 변경 여부 확인 (`app/main.py`, `app/api/*`)
2. 공개 경로 충돌 여부 확인 (legacy alias 포함)
3. 응답 shape drift 확인 (`success/data` vs direct response_model)
4. 문서-코드 정합성 (`백엔드_함수_분석_및_역할_분석_명세서.md` Canonical 수치)

### B. 기능 회귀 (권장 우선순위)

1. 인증
   - `/api/auth/google/login`, `/api/auth/google/callback`, `/api/auth/guest/login`
   - `/api/auth/me`, `/api/auth/logout`
2. 채팅
   - `/api/chat` 일반/감독 모드
   - `/api/chat/stream` SSE 완료/에러 프레임
3. TTS
   - `/api/tts` JSON / binary
   - `/api/voices/{voice_id}/test`
4. 음성 관리
   - `/api/voices` CRUD
   - `/api/voices/server-files*`
5. model-make
   - upload/start/status/log/register/abort/my/delete
6. 캐릭터
   - preset/dev/user/admin/generate 전 경로
7. 헬스체크
   - `/api/system/health/detailed`, `/api/health`

### C. 보안/설정 토글 검증

- `DEV_AUTH_FALLBACK_ENABLED=true/false`
- `GOOGLE_SSO_ALLOW_INSECURE_HTTP`
- `AUTH_COOKIE_SECURE`, `AUTH_COOKIE_SAMESITE`
- `STARTUP_SCHEMA_PATCH_ENABLED`
- `STRICT_STARTUP_VALIDATION` (warn-only)

## 변경 이력 / 후속 과제

### 이번 라운드 작성 메모

- `백엔드_함수_분석_및_역할_분석_명세서.md`의 라우터 인벤토리/계약/호환성 메모를 API 소비자 관점으로 재구성함
- 엔드포인트는 전체 인벤토리(54 + 앱 직접 5)를 기준으로 정리함
- guest 서버 인증(`guest_sessions`, `auth_events`, `/api/auth/guest/login`, `/api/auth/me` guest 응답)을 반영함
- admin 보호 설명에 백엔드 최종 권한 판정 + 프론트 `/admin/voices` UX 가드 추가 상태를 반영함
- 도메인별 응답 스타일 차이(래퍼형/직접형/SSE/바이너리/리다이렉트)를 명시함

### 후속 과제 (API 문서 관점)

1. 환경 준비 후 `/api/openapi.json` 기반 자동 스키마 추출 결과와 본 문서 수기 설명을 diff 검증
2. `voices/server-files` 및 `model-make`의 raw proxy 응답 예시를 실서비스 샘플(JSON)로 보강
3. `/api/chat`, `/api/chat/stream`, `/api/tts` 대표 요청/응답 예시를 실제 프론트 페이로드 기준으로 추가
4. 운영 배포 환경(도메인/프록시)별 Base URL 매트릭스를 별도 부록으로 분리
