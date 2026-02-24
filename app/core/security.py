from datetime import datetime, timedelta
from typing import Any, Union
from jose import jwt
from passlib.context import CryptContext
from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
GUEST_SUBJECT_PREFIX = "guest:"

def create_access_token(subject: Union[str, Any], expires_delta: timedelta = None) -> str:
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode = {"sub": str(subject), "exp": expire}
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def create_guest_access_token(subject: Union[str, Any], expires_delta: timedelta = None) -> str:
    guest_subject = f"{GUEST_SUBJECT_PREFIX}{subject}"
    effective_expires = expires_delta or timedelta(minutes=settings.GUEST_ACCESS_TOKEN_EXPIRE_MINUTES)
    return create_access_token(subject=guest_subject, expires_delta=effective_expires)
