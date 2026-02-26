from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Literal
import logging

from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.core.config import settings
from app.core.security import GUEST_SUBJECT_PREFIX
from app.models.user import User
from app.models.guest_session import GuestSession
from app.services import auth_principal_data_gateway

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login/access-token", auto_error=False)
logger = logging.getLogger(__name__)


@dataclass
class AuthPrincipal:
    kind: Literal["user", "guest"]
    user: Optional[User] = None
    guest_session: Optional[GuestSession] = None


def _decode_subject_from_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        return None
    subject = payload.get("sub")
    return str(subject) if subject else None


def _parse_subject(subject: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """
    Returns (kind, id).
    kind: "user" | "guest" | None
    """
    if not subject:
        return None, None
    if subject.startswith(GUEST_SUBJECT_PREFIX):
        guest_id = subject[len(GUEST_SUBJECT_PREFIX) :].strip()
        return ("guest", guest_id or None)
    return ("user", subject)


async def get_token_from_request(request: Request) -> Optional[str]:
    """쿠키(사용자 토큰) 또는 Authorization 헤더에서 토큰 추출."""
    token = request.cookies.get(settings.auth_cookie_name)
    if token:
        return token

    authorization = request.headers.get("Authorization")
    if authorization and authorization.startswith("Bearer "):
        return authorization.split(" ", 1)[1]

    return None


async def get_guest_token_from_request(request: Request) -> Optional[str]:
    """게스트 전용 쿠키에서 토큰 추출."""
    if not settings.GUEST_AUTH_ENABLED:
        return None
    return request.cookies.get(settings.guest_auth_cookie_name)


async def get_any_auth_tokens_from_request(request: Request) -> tuple[Optional[str], Optional[str]]:
    return await get_token_from_request(request), await get_guest_token_from_request(request)


async def _get_user_from_token(*, token: Optional[str], db: AsyncSession) -> Optional[User]:
    if not token:
        return None
    subject = _decode_subject_from_token(token)
    subject_kind, subject_id = _parse_subject(subject)
    if subject_kind != "user" or not subject_id:
        return None
    return await auth_principal_data_gateway.get_user_by_id(db=db, user_id=subject_id)


async def _get_guest_session_from_token(
    *,
    token: Optional[str],
    db: AsyncSession,
    mark_expired: bool = False,
) -> Optional[GuestSession]:
    if not token:
        return None
    subject = _decode_subject_from_token(token)
    subject_kind, subject_id = _parse_subject(subject)
    if subject_kind != "guest" or not subject_id:
        return None

    guest_session = await auth_principal_data_gateway.get_guest_session_by_id(
        db=db, guest_session_id=subject_id
    )
    if not guest_session:
        return None

    if guest_session.status != "active":
        return None

    expires_at = guest_session.expires_at
    if expires_at and expires_at.replace(tzinfo=None) <= datetime.utcnow():
        if mark_expired:
            try:
                updated = await auth_principal_data_gateway.update_guest_session_status(
                    db=db,
                    guest_session_id=guest_session.id,
                    status_value="expired",
                    reason="expires_at_reached",
                )
                if updated is not None:
                    guest_session = updated
            except Exception:
                logger.exception("Failed to mark guest session expired guest_session_id=%s", guest_session.id)
        return None

    return guest_session


async def get_current_principal_optional(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Optional[AuthPrincipal]:
    user_token, guest_token = await get_any_auth_tokens_from_request(request)

    user = await _get_user_from_token(token=user_token, db=db)
    if user:
        return AuthPrincipal(kind="user", user=user)

    guest_session = await _get_guest_session_from_token(token=guest_token, db=db, mark_expired=True)
    if guest_session:
        return AuthPrincipal(kind="guest", guest_session=guest_session)

    return None


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    현재 사용자 조회 (쿠키 또는 Authorization 헤더에서 토큰 읽기)

    [주의]
    - guest 쿠키가 있는 요청은 user-only dependency에서 dev-user fallback로 내려가면 안 된다.
      (관리자 라우트 보호/권한 의미 혼선 방지)
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    user_token, guest_token = await get_any_auth_tokens_from_request(request)

    if user_token:
        subject = _decode_subject_from_token(user_token)
        subject_kind, subject_id = _parse_subject(subject)
        if subject_kind == "user" and subject_id:
            user = await auth_principal_data_gateway.get_user_by_id(db=db, user_id=subject_id)
            if user:
                return user
        elif subject_kind == "guest":
            logger.warning("Guest token provided to user-only dependency path=%s", request.url.path)
            raise credentials_exception

    if guest_token:
        logger.info("Guest auth cookie rejected by user-only dependency path=%s", request.url.path)
        raise credentials_exception

    if not settings.DEV_AUTH_FALLBACK_ENABLED:
        raise credentials_exception

    dev_user_id = "dev-user"
    result = await db.execute(select(User).where(User.id == dev_user_id))
    user = result.scalar_one_or_none()

    if not user:
        user = User(
            id=dev_user_id,
            email="dev@example.com",
            username="개발자",
            provider="local",
        )
        db.add(user)
        try:
            await db.commit()
            await db.refresh(user)
        except Exception:
            await db.rollback()
            result = await db.execute(select(User).where(User.id == dev_user_id))
            user = result.scalar_one_or_none()

    if user:
        logger.warning(
            "DEV auth fallback is enabled. Using dev-user fallback for request auth. path=%s user_id=%s",
            request.url.path,
            user.id,
        )
        return user

    raise credentials_exception


async def get_current_user_optional(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    """
    현재 사용자 조회. 토큰 없거나 만료/유효하지 않으면 None 반환 (HTTPException 미발생).
    chat 등 비로그인 허용 엔드포인트에서 사용.
    """
    token = await get_token_from_request(request)
    if not token:
        return None

    subject = _decode_subject_from_token(token)
    subject_kind, user_id = _parse_subject(subject)
    if subject_kind != "user" or not user_id:
        return None

    return await auth_principal_data_gateway.get_user_by_id(db=db, user_id=user_id)


async def require_admin(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> User:
    """
    관리자 권한 확인 의존성.
    ADMIN_EMAILS 환경변수에 등록된 이메일만 관리자로 인정.
    """
    if not current_user or not current_user.email:
        logger.warning("Admin access denied (no authenticated user/email) path=%s", request.url.path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="인증이 필요합니다",
        )
    if not settings.is_admin(current_user.email):
        logger.warning(
            "Admin access denied (non-admin) path=%s user_id=%s email=%s",
            request.url.path,
            current_user.id,
            current_user.email,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="관리자 권한이 필요합니다",
        )
    return current_user
