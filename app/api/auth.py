from datetime import datetime, timedelta
import uuid
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.responses import JSONResponse
from fastapi_sso.sso.google import GoogleSSO
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token, create_guest_access_token
from app.models.user import User
from app.models.guest_session import GuestSession
from app.api.deps import get_current_principal_optional, AuthPrincipal
from app.services.auth_audit_service import log_auth_event

router = APIRouter()
logger = logging.getLogger(__name__)

sso = GoogleSSO(
    client_id=settings.GOOGLE_CLIENT_ID,
    client_secret=settings.GOOGLE_CLIENT_SECRET,
    redirect_uri=settings.GOOGLE_REDIRECT_URI,
    allow_insecure_http=settings.GOOGLE_SSO_ALLOW_INSECURE_HTTP
)
# [보안 민감]
# 1차 리팩토링 정책상 insecure HTTP 허용 여부는 Settings/env 플래그로 제어하고,
# 기본값은 기존 동작 호환을 위해 유지한다.


class GuestLoginRequest(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=50)


def _sanitize_guest_display_name(raw_name: Optional[str]) -> str:
    cleaned = (raw_name or "").strip()
    if not cleaned:
        return "게스트"
    return cleaned[:50]


def _build_auth_principal_response(principal: AuthPrincipal) -> dict:
    if principal.kind == "user" and principal.user:
        user = principal.user
        return {
            "id": user.id,
            "email": user.email,
            "username": user.username,
            "picture": user.picture,
            "provider": user.provider,
            "is_admin": settings.is_admin(user.email),
            "auth_mode": "google",
            "is_guest": False,
        }

    if principal.kind == "guest" and principal.guest_session:
        guest_session = principal.guest_session
        display_name = guest_session.display_name or "게스트"
        return {
            "id": guest_session.id,
            "email": None,
            "username": display_name,
            "picture": None,
            "provider": "guest",
            "is_admin": False,
            "auth_mode": "guest",
            "is_guest": True,
        }

    raise HTTPException(status_code=500, detail="인증 주체 응답 생성 실패")


def _set_guest_auth_cookie(response: JSONResponse, *, token: str) -> None:
    cookie_kwargs = {
        "key": settings.guest_auth_cookie_name,
        "value": token,
        "httponly": True,
        "secure": settings.AUTH_COOKIE_SECURE,
        "samesite": settings.auth_cookie_samesite_value,
        "path": "/",
    }
    if not settings.GUEST_COOKIE_SESSION_ONLY:
        cookie_kwargs["max_age"] = settings.GUEST_ACCESS_TOKEN_EXPIRE_MINUTES * 60
    response.set_cookie(**cookie_kwargs)


async def _verify_google_user_info_or_raise(request: Request) -> Any:
    """
    [역할]
    Google SSO 콜백 요청을 검증하고 사용자 정보를 반환한다.

    [왜 helper로 분리하나]
    `google_callback` 내부에서 타임아웃/SSO 예외 처리 블록이 길어져 가독성이 떨어지므로,
    외부 계약을 바꾸지 않고 단계만 분리한다.
    """
    import asyncio

    try:
        # Google SSO 검증 (타임아웃 30초로 증가 - 느린 네트워크 대응)
        try:
            return await asyncio.wait_for(
                sso.verify_and_process(request),
                timeout=30.0,  # 10초 → 30초로 증가
            )
        except asyncio.TimeoutError:
            logger.warning("Google 인증 타임아웃 (30초 초과)")
            raise HTTPException(
                status_code=408,
                detail="Google 인증 응답 시간이 초과되었습니다. 네트워크 연결을 확인하고 다시 시도해주세요."
            )
    except HTTPException:
        raise
    except Exception as e:
        # broad except 유지 이유:
        # fastapi-sso/provider 라이브러리 예외 타입이 환경/버전별로 다양해 1차는 400 매핑을 고정한다.
        logger.error("Google 인증 실패: %s", str(e), exc_info=True)
        raise HTTPException(
            status_code=400,
            detail=f"Google 인증 실패: {str(e)}"
        )


async def _get_or_create_google_user_or_raise(*, db: AsyncSession, user_info: Any) -> User:
    """
    [역할]
    Google 사용자 정보 기준으로 DB 사용자 조회/생성을 수행하고 `User`를 반환한다.

    [왜 여기서 처리하나]
    `google_callback`의 핵심은 OAuth 응답 검증 -> 사용자 식별 -> 토큰 발급 -> 쿠키 응답 조립 순서다.
    그중 DB 조회/생성 단계는 예외 정책(`500` detail 형식 유지)과 rollback 규칙이 섞여 길어지므로 helper로 고정한다.

    [입력/출력]
    - 입력: 현재 DB 세션, `fastapi-sso`가 반환한 `user_info` 객체
    - 출력: 기존 사용자 또는 새로 생성된 `User` ORM 객체

    [부작용]
    - DB 조회 1회
    - 사용자 미존재 시 INSERT + commit/refresh
    - 로그 기록

    [실패/예외]
    - 조회/생성 실패는 기존 계약 유지 차원에서 `HTTPException(500)`으로 매핑
    - 생성 경로만 rollback 수행

    [주의]
    - 조회/생성 실패 시 외부 응답은 기존과 동일한 500 메시지 형식을 유지한다.
    - 생성 경로에서만 `rollback()`을 수행한다 (조회 경로는 트랜잭션 write 없음).
    """
    user_email = user_info.email

    # Check if user exists (인덱스 활용 - email 컬럼에 인덱스 필요)
    try:
        result = await db.execute(select(User).where(User.email == user_email))
        user = result.scalar_one_or_none()
    except SQLAlchemyError as e:
        logger.error("사용자 조회 실패: %s", str(e), exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="사용자 정보 조회 중 오류가 발생했습니다."
        )
    except Exception as e:
        # broad except 유지 이유: SQLAlchemy 외 세션 상태/드라이버 예외도 같은 500 정책으로 통일
        logger.error("사용자 조회 실패(비정형 예외): %s", str(e), exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="사용자 정보 조회 중 오류가 발생했습니다."
        )

    if user:
        return user

    # Create new user (최소한의 필드만 설정하여 성능 최적화)
    try:
        user = User(
            id=str(uuid.uuid4()),
            email=user_email,
            username=user_info.display_name or user_email.split("@")[0],  # display_name이 없으면 이메일 앞부분 사용
            picture=user_info.picture,
            provider="google"
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        logger.info("새 사용자 생성: %s", user_email)
        return user
    except SQLAlchemyError as e:
        logger.error("사용자 생성 실패: %s", str(e), exc_info=True)
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail="사용자 생성 중 오류가 발생했습니다."
        )
    except Exception as e:
        # broad except 유지 이유: ORM 상태/입력 객체 이상 등 비정형 예외도 동일 500 응답을 유지한다.
        logger.error("사용자 생성 실패(비정형 예외): %s", str(e), exc_info=True)
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail="사용자 생성 중 오류가 발생했습니다."
        )


def _create_access_token_or_raise(*, user_id: str) -> str:
    """
    [역할]
    JWT 액세스 토큰을 생성하고 실패 시 기존 정책의 500 에러로 매핑한다.

    [왜 여기서 처리하나]
    JWT 생성 실패는 보통 설정/서명키/라이브러리 상태 문제라 `google_callback` 본문에서 상세 분기할 가치가 낮다.
    토큰 발급 정책과 에러 매핑을 helper에 고정하면 라우터는 단계 순서만 읽히게 유지할 수 있다.

    [주의]
    - 1차 리팩토링에서는 실패 원인을 외부 detail로 세분화하지 않는다(보안/호환성 이유).
    - 토큰 payload/만료 정책은 `create_access_token`과 `settings`가 단일 진실 원천이다.
    """
    try:
        return create_access_token(subject=user_id)
    except Exception as e:
        # broad except 유지 이유: JWT 라이브러리/설정 오류를 외부에 세분화 노출하지 않고 500으로 고정한다.
        logger.error("JWT 생성 실패: %s", str(e), exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="인증 토큰 생성 중 오류가 발생했습니다."
        )


def _build_auth_callback_redirect_response(*, access_token: str) -> RedirectResponse:
    """
    [역할]
    프론트 콜백 리다이렉트 응답을 만들고 HttpOnly 쿠키를 설정한다.

    [왜 여기서 처리하나]
    `google_callback`의 마지막 단계는 "리다이렉트 URL 조립 + 쿠키 옵션 설정"이라는 transport 책임이다.
    이 블록을 분리하면 라우터 본문에서 인증 흐름(검증/DB/JWT)과 응답 조립 책임을 구분해 읽기 쉬워진다.

    [주의]
    - 쿠키 보안 속성은 Settings를 통해 제어하며, 1차 리팩토링에서는 기본값만 기존 동작 호환을 유지한다.
    - `frontend_base_url`/쿠키명/path는 프론트 로그인 완료 동선 계약이라 1차에서 변경 금지다.
    """
    frontend_url = settings.frontend_base_url
    redirect_url = f"{frontend_url}/auth/callback"
    response = RedirectResponse(url=redirect_url)

    # Set HttpOnly Cookie for JWT token
    # [보안 민감]
    # 1차 리팩토링에서는 기존 동작(secure=False, samesite=lax)을 기본값으로 유지한다.
    # 대신 하드코딩을 Settings로 올려서 운영에서 env만 바꿔도 조정 가능하게 만든다.
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=access_token,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.auth_cookie_samesite_value,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/"
    )
    # Google 로그인 성공 시 남아있을 수 있는 guest 쿠키는 제거해 세션 혼선을 방지한다.
    response.delete_cookie(
        key=settings.guest_auth_cookie_name,
        path="/",
        samesite=settings.auth_cookie_samesite_value,
        secure=settings.AUTH_COOKIE_SECURE,
    )
    return response

@router.get("/google/login")
async def google_login():
    """Redirect user to Google Login"""
    # [왜 여기서 처리하나]
    # 로그인 시작 엔드포인트는 provider SDK 리다이렉트만 담당한다. 추가 로직을 섞으면 callback 단계와 책임이 흐려진다.
    return await sso.get_login_redirect()


@router.post("/guest/login")
async def guest_login(
    body: GuestLoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    프론트 게스트 시작 요청을 서버 guest 세션/쿠키로 확장한다.
    """
    if not settings.GUEST_AUTH_ENABLED:
        await log_auth_event(
            db,
            request=request,
            actor_type="guest",
            actor_id=None,
            provider="guest",
            event_type="login",
            success=False,
            reason="guest_auth_disabled",
            commit=True,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Guest auth is disabled")

    display_name = _sanitize_guest_display_name(body.display_name)
    now = datetime.utcnow()
    guest_session = GuestSession(
        id=str(uuid.uuid4()),
        display_name=display_name,
        status="active",
        last_seen_at=now,
        expires_at=now + timedelta(minutes=settings.GUEST_ACCESS_TOKEN_EXPIRE_MINUTES),
        client_ip=request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else None),
        user_agent=request.headers.get("user-agent"),
    )
    db.add(guest_session)
    try:
        await db.commit()
        await db.refresh(guest_session)
    except Exception:
        await db.rollback()
        logger.exception("Failed to create guest session")
        await log_auth_event(
            db,
            request=request,
            actor_type="guest",
            actor_id=None,
            provider="guest",
            event_type="login",
            success=False,
            reason="db_error",
            commit=True,
        )
        raise HTTPException(status_code=500, detail="게스트 세션 생성에 실패했습니다.")

    token = create_guest_access_token(subject=guest_session.id)
    response_payload = _build_auth_principal_response(AuthPrincipal(kind="guest", guest_session=guest_session))
    response = JSONResponse(content=response_payload)
    _set_guest_auth_cookie(response, token=token)
    await log_auth_event(
        db,
        request=request,
        actor_type="guest",
        actor_id=guest_session.id,
        provider="guest",
        event_type="login",
        success=True,
        reason=None,
        commit=True,
    )
    return response

@router.get("/google/callback")
async def google_callback(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Process login response from Google and return JWT
    
    성능 최적화:
    - Google SSO 타임아웃: 30초 (느린 네트워크 대응)
    - 데이터베이스 쿼리 최적화 (인덱스 활용)
    - 에러 처리 개선
    """
    # [왜 라우터에서 단계 helper를 순서대로만 호출하나]
    # 1차 리팩토링 목표는 외부 계약(`/google/callback`, 쿠키/리다이렉트 동작) 유지이므로,
    # 오케스트레이션 순서만 라우터에 남기고 각 단계의 상세 예외 정책은 helper로 고정한다.
    #
    # [주의]
    # 아래 단계 순서를 바꾸면 "미검증 user_info -> DB 접근 -> JWT 발급 -> 쿠키 설정" 계약이 깨질 수 있다.
    # 1) Google SSO 콜백 검증 (timeout 포함)
    user_info = await _verify_google_user_info_or_raise(request)
    
    if not user_info:
        raise HTTPException(status_code=400, detail="Failed to get user info")

    # 2) 사용자 조회/생성 (DB)
    user = await _get_or_create_google_user_or_raise(db=db, user_info=user_info)

    # 3) JWT 생성
    access_token = _create_access_token_or_raise(user_id=user.id)

    # 4) 프론트 리다이렉트 + HttpOnly 쿠키 설정
    await log_auth_event(
        db,
        request=request,
        actor_type="google_user",
        actor_id=user.id,
        provider="google",
        event_type="login",
        success=True,
        commit=True,
    )
    return _build_auth_callback_redirect_response(access_token=access_token)

@router.get("/me")
async def get_current_user_info(
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Optional[AuthPrincipal] = Depends(get_current_principal_optional),
):
    """현재 로그인한 사용자(google/guest) 정보 조회"""
    # [왜 여기서 처리하나]
    # 프론트 초기 부팅 시 `/auth/me`는 자주 호출되므로 최소 필드만 반환해 응답 크기와 직렬화 비용을 줄인다.
    #
    # [주의]
    # 프론트가 사용하는 키(id/email/username/picture/provider/is_admin)는 1차 리팩토링에서 유지한다.
    # 빠른 응답을 위해 최소한의 데이터만 반환
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if principal.kind == "guest" and principal.guest_session:
        principal.guest_session.last_seen_at = datetime.utcnow()
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("Failed to update guest_session.last_seen_at guest_session_id=%s", principal.guest_session.id)

    return _build_auth_principal_response(principal)

@router.post("/logout")
async def logout(
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Optional[AuthPrincipal] = Depends(get_current_principal_optional),
):
    """로그아웃 - 쿠키에서 토큰 삭제"""
    # [왜 여기서 처리하나]
    # 로그아웃은 서버 상태 변경보다 "쿠키 제거 + 프론트 복귀"가 핵심이므로 RedirectResponse 조립을 이 라우터에서 직접 처리한다.
    #
    # [주의]
    # delete_cookie 옵션(path/samesite/secure)은 set_cookie와 일치해야 브라우저가 동일 쿠키를 정확히 제거한다.
    frontend_url = settings.frontend_base_url
    redirect_url = f"{frontend_url}/"
    
    if principal and principal.kind == "guest" and principal.guest_session:
        principal.guest_session.status = "logged_out"
        principal.guest_session.ended_at = datetime.utcnow()
        principal.guest_session.last_seen_at = datetime.utcnow()
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("Failed to mark guest logout guest_session_id=%s", principal.guest_session.id)
        await log_auth_event(
            db,
            request=request,
            actor_type="guest",
            actor_id=principal.guest_session.id,
            provider="guest",
            event_type="logout",
            success=True,
            commit=True,
        )
    elif principal and principal.kind == "user" and principal.user:
        await log_auth_event(
            db,
            request=request,
            actor_type="google_user",
            actor_id=principal.user.id,
            provider=principal.user.provider or "google",
            event_type="logout",
            success=True,
            commit=True,
        )

    response = RedirectResponse(url=redirect_url)
    # 쿠키 삭제
    response.delete_cookie(
        key=settings.auth_cookie_name,
        path="/",
        samesite=settings.auth_cookie_samesite_value,
        secure=settings.AUTH_COOKIE_SECURE,
    )
    response.delete_cookie(
        key=settings.guest_auth_cookie_name,
        path="/",
        samesite=settings.auth_cookie_samesite_value,
        secure=settings.AUTH_COOKIE_SECURE,
    )
    return response
