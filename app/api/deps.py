from typing import Generator, Optional
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.config import settings
from app.models.user import User
from sqlalchemy import select

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login/access-token", auto_error=False)

async def get_token_from_request(request: Request) -> Optional[str]:
    """쿠키 또는 Authorization 헤더에서 토큰 추출"""
    # 1. 쿠키에서 토큰 확인 (우선순위)
    token = request.cookies.get("access_token")
    if token:
        return token
    
    # 2. Authorization 헤더에서 토큰 확인 (하위 호환성)
    authorization = request.headers.get("Authorization")
    if authorization and authorization.startswith("Bearer "):
        return authorization.split(" ")[1]
    
    return None

async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db)
) -> User:
    """현재 사용자 조회 (쿠키 또는 Authorization 헤더에서 토큰 읽기)"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    # 쿠키 또는 헤더에서 토큰 가져오기
    token = await get_token_from_request(request)

    # 1. 토큰이 있으면 검증 시도
    if token:
        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
            user_id: str = payload.get("sub")
            if user_id:
                result = await db.execute(select(User).where(User.id == user_id))
                user = result.scalar_one_or_none()
                if user:
                    return user
        except JWTError:
            # 토큰이 만료되었거나 유효하지 않으면 폴백으로 이동
            pass
    
    # [복구] 개발 환경 폴백: 토큰이 없거나 유효하지 않으면 dev-user 반환
    dev_user_id = "dev-user"
    result = await db.execute(select(User).where(User.id == dev_user_id))
    user = result.scalar_one_or_none()
    
    if not user:
        # DB에 dev-user가 없으면 생성
        user = User(
            id=dev_user_id,
            email="dev@example.com",
            username="개발자",
            provider="local"
        )
        db.add(user)
        try:
            await db.commit()
            await db.refresh(user)
        except Exception:
            await db.rollback()
            # 동시성 문제 등으로 실패 시 다시 조회
            result = await db.execute(select(User).where(User.id == dev_user_id))
            user = result.scalar_one_or_none()

    if user:
         print(f"DEBUG: Using dev-user fallback for auth. ID={user.id}")
         return user

    raise credentials_exception


async def get_current_user_optional(
    request: Request,
    db: AsyncSession = Depends(get_db)
) -> Optional[User]:
    """
    현재 사용자 조회. 토큰 없거나 만료/유효하지 않으면 None 반환 (HTTPException 미발생).
    chat 등 비로그인 허용 엔드포인트에서 사용.
    """
    token = await get_token_from_request(request)
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            return None
    except JWTError:
        return None
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    return user


async def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """
    관리자 권한 확인 의존성.
    ADMIN_EMAILS 환경변수에 등록된 이메일만 관리자로 인정.
    """
    if not current_user or not current_user.email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="인증이 필요합니다",
        )
    if not settings.is_admin(current_user.email):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="관리자 권한이 필요합니다",
        )
    return current_user
